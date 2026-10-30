<template>
  <div>
    <div class="panel">
      <h2>Profile</h2>
      <p>Email: <strong>{{ me.email }}</strong></p>
      <p>Zabbix API token: {{ me.token_configured ? "configured" : "not configured" }}</p>
    </div>

    <div class="panel">
      <h3>Change email</h3>
      <p class="muted">You can use another email address configured for your Zabbix user. Subscriptions are kept.</p>
      <button v-if="!emails" :disabled="busy" @click="loadEmails">Show available addresses</button>
      <form v-else @submit.prevent="changeEmail">
        <div v-for="item in emails" :key="item.email">
          <label style="font-weight: normal">
            <input v-model="newEmail" type="radio" name="new-email" :value="item.email" :disabled="!item.available" />
            {{ item.email }}
            <span v-if="item.current" class="muted">(current)</span>
            <span v-else-if="!item.available" class="muted">(used by another profile)</span>
          </label>
        </div>
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy || !newEmail">Change email</button>
          <button type="button" @click="emails = null">Cancel</button>
        </div>
      </form>
      <p v-if="emailMessage" class="success">{{ emailMessage }}</p>
      <p v-if="emailError" class="error">{{ emailError }}</p>
    </div>

    <div class="panel">
      <h3>Replace Zabbix API token</h3>
      <form @submit.prevent="changeToken">
        <label for="new-token">New API token</label>
        <input id="new-token" v-model.trim="token" type="password" autocomplete="off" required />
        <p class="hint">The token must belong to the same Zabbix user. The old token is kept if the new one is rejected.</p>
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy">Save token</button>
        </div>
      </form>
      <p v-if="tokenMessage" class="success">{{ tokenMessage }}</p>
      <p v-if="tokenError" class="error">{{ tokenError }}</p>
    </div>

    <div v-if="me.is_admin && !me.impersonating" class="panel">
      <h3>Impersonate</h3>
      <form @submit.prevent="impersonate">
        <label for="imp-email">User email</label>
        <input id="imp-email" v-model.trim="impEmail" type="email" required />
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy">Impersonate</button>
        </div>
      </form>
      <p v-if="impError" class="error">{{ impError }}</p>
    </div>

    <div class="panel">
      <h3>Sessions</h3>
      <p class="muted">Log out on all devices and browsers, including this one.</p>
      <button :disabled="busy" @click="logoutAll">Log out all sessions</button>
      <p v-if="sessionError" class="error">{{ sessionError }}</p>
    </div>

    <div class="panel">
      <h3>Delete profile</h3>
      <p class="muted">Deletes the profile and all subscriptions. Type DELETE to confirm.</p>
      <form @submit.prevent="deleteProfile">
        <input v-model="confirmText" type="text" autocomplete="off" />
        <div class="actions">
          <button class="danger" type="submit" :disabled="busy || confirmText !== 'DELETE'">Delete profile</button>
        </div>
      </form>
      <p v-if="deleteError" class="error">{{ deleteError }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";

const props = defineProps({ me: { type: Object, required: true } });
const emit = defineEmits(["changed", "logged-out"]);

const busy = ref(false);
const emails = ref(null);
const newEmail = ref("");
const emailMessage = ref("");
const emailError = ref("");
const token = ref("");
const tokenMessage = ref("");
const tokenError = ref("");
const impEmail = ref("");
const impError = ref("");
const sessionError = ref("");
const confirmText = ref("");
const deleteError = ref("");

async function guarded(errorRef, action) {
  busy.value = true;
  errorRef.value = "";
  try {
    await action();
  } catch (e) {
    errorRef.value = e.message;
  } finally {
    busy.value = false;
  }
}

function loadEmails() {
  guarded(emailError, async () => {
    emails.value = (await api.get("/api/profile/emails")).emails;
    newEmail.value = "";
  });
}

function changeEmail() {
  emailMessage.value = "";
  guarded(emailError, async () => {
    const result = await api.post("/api/profile/email", { email: newEmail.value });
    emailMessage.value = `Email changed to ${result.email}.`;
    emails.value = null;
    emit("changed");
  });
}

function changeToken() {
  tokenMessage.value = "";
  guarded(tokenError, async () => {
    await api.post("/api/profile/token", { token: token.value });
    token.value = "";
    tokenMessage.value = "Token saved.";
  });
}

function impersonate() {
  guarded(impError, async () => {
    await api.post("/api/admin/impersonate", { email: impEmail.value });
    impEmail.value = "";
    emit("changed");
  });
}

function logoutAll() {
  if (!window.confirm("Log out all sessions?")) return;
  guarded(sessionError, async () => {
    await api.post("/api/auth/logout-all");
    if (props.me.impersonating) emit("changed");
    else emit("logged-out");
  });
}

function deleteProfile() {
  guarded(deleteError, async () => {
    await api.post("/api/profile/delete", { confirm: confirmText.value });
    if (props.me.impersonating) emit("changed");
    else emit("logged-out");
  });
}
</script>
