import { createApp } from "vue";
import * as Sentry from "@sentry/vue";
import App from "./App.vue";
import { api } from "./api.js";
import "./style.css";

async function start() {
  const app = createApp(App);
  let config = {};
  try {
    config = await api.get("/api/public-config");
    if (config.sentry_dsn) {
      Sentry.init({
        app,
        dsn: config.sentry_dsn,
        environment: config.sentry_environment || undefined,
        release: config.version,
        // Errors only: no tracing, no replay, no default PII.
        tracesSampleRate: 0,
        sendDefaultPii: false,
        integrations: [],
        beforeSend(event) {
          // Login and unsubscribe tokens are part of the URL path.
          if (event.request && event.request.url) {
            event.request.url = event.request.url.replace(/\/(login|unsubscribe)\/[^/?#]+/, "/$1/***");
          }
          return event;
        },
      });
    }
  } catch (e) {
    // The UI works without the public configuration.
  }
  app.provide("publicConfig", config || {});
  app.mount("#app");
}

start();
