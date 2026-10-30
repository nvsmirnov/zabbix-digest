<template>
  <div>
    <div v-if="notice" class="banner">{{ notice }}</div>

    <div class="panel">
      <h2>Log in with email</h2>
      <p class="muted">For registered users. A one-time login link is sent to your address.</p>
      <form @submit.prevent="sendLink">
        <label for="login-email">Email</label>
        <input id="login-email" v-model.trim="email" type="email" autocomplete="email" required />
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy">Send login link</button>
        </div>
      </form>
      <p v-if="linkMessage" class="success">{{ linkMessage }}</p>
      <p v-if="linkError" class="error">{{ linkError }}</p>
    </div>

    <div class="panel">
      <h2>Register or log in with a Zabbix API token</h2>
      <p class="muted">
        Create an API token in Zabbix: User settings - API tokens. The token is stored by the service and used to
        read your Problems widgets with your own Zabbix permissions. It is never shown again.
      </p>
      <form v-if="!emails" @submit.prevent="checkToken">
        <label for="login-token">Zabbix API token</label>
        <input id="login-token" v-model.trim="token" type="password" autocomplete="off" required />
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy">Continue</button>
        </div>
      </form>
      <form v-else @submit.prevent="finishToken">
        <label>Email address for digests</label>
        <div v-for="item in emails" :key="item.email">
          <label style="font-weight: normal">
            <input v-model="chosen" type="radio" name="email" :value="item.email" />
            {{ item.email }}
            <span v-if="item.registered" class="muted">(registered - you will log in to this profile)</span>
          </label>
        </div>
        <p class="hint">Only email addresses configured as media for your Zabbix user can be used.</p>
        <div class="actions">
          <button class="primary" type="submit" :disabled="busy || !chosen">Continue</button>
          <button type="button" @click="emails = null">Back</button>
        </div>
      </form>
      <p v-if="tokenError" class="error">{{ tokenError }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";

const props = defineProps({ notice: { type: String, default: "" } });
const emit = defineEmits(["done"]);

const STORAGE_KEY = "zabbix-digest-email";

function rememberedEmail() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || "";
  } catch (e) {
    return "";
  }
}

const email = ref(rememberedEmail());
const token = ref("");
const emails = ref(null);
const chosen = ref("");
const busy = ref(false);
const linkMessage = ref("");
const linkError = ref("");
const tokenError = ref("");

function remember(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch (e) {
    // Storage may be unavailable.
  }
}

async function sendLink() {
  busy.value = true;
  linkMessage.value = "";
  linkError.value = "";
  try {
    const result = await api.post("/api/auth/email", { email: email.value });
    remember(email.value);
    linkMessage.value = result.message;
  } catch (e) {
    linkError.value = e.message;
  } finally {
    busy.value = false;
  }
}

async function checkToken() {
  busy.value = true;
  tokenError.value = "";
  try {
    const result = await api.post("/api/auth/token", { token: token.value });
    emails.value = result.emails;
    const remembered = result.emails.find((e) => e.email.toLowerCase() === email.value.toLowerCase());
    chosen.value = remembered ? remembered.email : result.emails.length === 1 ? result.emails[0].email : "";
  } catch (e) {
    tokenError.value = e.message;
  } finally {
    busy.value = false;
  }
}

async function finishToken() {
  busy.value = true;
  tokenError.value = "";
  try {
    await api.post("/api/auth/token", { token: token.value, email: chosen.value });
    remember(chosen.value);
    token.value = "";
    emit("done");
  } catch (e) {
    tokenError.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
