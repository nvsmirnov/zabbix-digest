"""External program adapter.

The program receives the structured message as JSON on stdin. No shell is used.
Exit codes: 0 delivered; 75 (EX_TEMPFAIL) temporary failure, retried; anything else permanent failure.
"""

import asyncio
import json
import logging
import os

from pydantic import Field, ValidationError

from common.errors import describe_error
from common.logs import REDACTOR
from common.settings import StrictModel, format_validation_error
from ..config import CoreSettings
from ..errors import PermanentTransportError, TransportError

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_TEMPFAIL = 75


class ExternalSettings(StrictModel):
    # Program and arguments, e.g. ["/opt/sender/send.py", "--channel", "ops"].
    command: list[str] = Field(min_length=1)
    # Extra environment variables. The Core environment is not passed except PATH, LANG, TZ.
    env: dict[str, str] = {}
    pass_environment: list[str] = ["PATH", "LANG", "LC_ALL", "TZ"]
    working_dir: str | None = None
    # Log the first characters of stderr on failure. Off by default: output may contain secrets.
    log_stderr: bool = False
    log_stderr_limit: int = 500


class ExternalAdapter:
    supports_preview = False

    def __init__(self, name: str, settings: dict, core: CoreSettings) -> None:
        try:
            self.settings = ExternalSettings.model_validate(settings or {})
        except ValidationError as exc:
            raise ValueError("; ".join(format_validation_error(exc))) from None
        self.name = name

    def render_preview(self, message: dict) -> dict:
        raise NotImplementedError

    def _environment(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in self.settings.pass_environment if k in os.environ}
        env.update(self.settings.env)
        return env

    async def send(self, message: dict) -> None:
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.settings.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
                cwd=self.settings.working_dir,
            )
        except OSError as exc:
            raise PermanentTransportError(f"cannot start external sender: {describe_error(exc)}") from None
        try:
            _, stderr = await proc.communicate(data)
        except asyncio.CancelledError:
            # Timeout or shutdown deadline: do not leave the process running.
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        code = proc.returncode
        if code == EXIT_OK:
            return
        if self.settings.log_stderr and stderr:
            text = stderr[: self.settings.log_stderr_limit].decode("utf-8", "replace")
            log.warning("external sender stderr: %s", REDACTOR.redact(text), extra={"transport": self.name})
        if code == EXIT_TEMPFAIL:
            raise TransportError(f"external sender reported a temporary failure (exit code {code})")
        raise PermanentTransportError(f"external sender failed (exit code {code})")
