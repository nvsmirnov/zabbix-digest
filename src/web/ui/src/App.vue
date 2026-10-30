<template>
  <div class="container">
    <header class="top">
      <h1>Zabbix Digest</h1>
      <nav v-if="me" class="actions" style="margin-top: 0; align-items: center">
        <span class="muted" :title="me.impersonating ? `Signed in as ${me.real_email}` : ''">{{ me.email }}</span>
        <button :class="{ active: tab === 'subscriptions' }" @click="tab = 'subscriptions'">Subscriptions</button>
        <button :class="{ active: tab === 'profile' }" @click="tab = 'profile'">Profile</button>
        <button @click="logout">Log out</button>
        <a v-if="helpUrl" class="button" :href="helpUrl" target="_blank" rel="noopener">Help</a>
      </nav>
    </header>

    <LinkLogin v-if="route.name === 'link'" :token="route.token" @done="afterLogin" />
    <Unsubscribe v-else-if="route.name === 'unsubscribe'" :token="route.token" />
    <template v-else>
      <p v-if="loading" class="muted">Loading...</p>
      <div v-else-if="loadError" class="panel">
        <p class="error">{{ loadError }}</p>
        <button @click="load">Retry</button>
      </div>
      <template v-else-if="!me">
        <Login @done="afterLogin" />
        <div v-if="helpUrl" class="actions">
          <a class="button" :href="helpUrl" target="_blank" rel="noopener">Help</a>
        </div>
      </template>
      <template v-else>
        <div v-if="me.impersonating" class="banner">
          <span>
            You are acting as <strong>{{ me.email }}</strong> (signed in as {{ me.real_email }}).
          </span>
          <button @click="stopImpersonation">Stop impersonation</button>
        </div>
        <Subscriptions v-if="tab === 'subscriptions'" :me="me" @changed="load" />
        <Profile v-else :me="me" @changed="load" @logged-out="loggedOut" />
      </template>
    </template>
  </div>
</template>

<script setup>
import { inject, onMounted, ref } from "vue";
import { api } from "./api.js";
import Login from "./views/Login.vue";
import LinkLogin from "./views/LinkLogin.vue";
import Unsubscribe from "./views/Unsubscribe.vue";
import Subscriptions from "./views/Subscriptions.vue";
import Profile from "./views/Profile.vue";

function parseRoute() {
  const path = window.location.pathname;
  let m = path.match(/^\/login\/([^/]+)$/);
  if (m) return { name: "link", token: decodeURIComponent(m[1]) };
  m = path.match(/^\/unsubscribe\/([^/]+)$/);
  if (m) return { name: "unsubscribe", token: decodeURIComponent(m[1]) };
  return { name: "main" };
}

const route = ref(parseRoute());
// Optional link to user help (web setting help_url); not shown when empty.
const helpUrl = (inject("publicConfig", {}) || {}).help_url || "";
const me = ref(null);
const tab = ref("subscriptions");
const loading = ref(false);
const loadError = ref("");

async function load() {
  loadError.value = "";
  try {
    me.value = await api.get("/api/me");
  } catch (e) {
    if (e.status === 401) {
      me.value = null;
    } else {
      loadError.value = e.message;
    }
  }
}

async function afterLogin() {
  window.history.replaceState(null, "", "/");
  route.value = { name: "main" };
  loading.value = true;
  await load();
  loading.value = false;
}

function loggedOut() {
  me.value = null;
  tab.value = "subscriptions";
}

async function logout() {
  try {
    await api.post("/api/auth/logout");
  } finally {
    loggedOut();
  }
}

async function stopImpersonation() {
  await api.post("/api/admin/stop-impersonation");
  tab.value = "subscriptions";
  await load();
}

onMounted(async () => {
  if (route.value.name === "main") {
    loading.value = true;
    await load();
    loading.value = false;
  }
});
</script>
