<template>
  <div class="panel">
    <h2>Unsubscribe</h2>
    <p v-if="loading" class="muted">Loading...</p>
    <template v-else-if="info">
      <p v-if="done || !info.enabled">
        You are unsubscribed from the digest <strong>{{ info.title }}</strong>. The subscription is disabled. You can
        enable it again after logging in.
      </p>
      <template v-else>
        <p>Do you want to stop receiving the digest <strong>{{ info.title }}</strong>?</p>
        <div class="actions">
          <button class="danger" :disabled="busy" @click="confirm">Unsubscribe</button>
        </div>
      </template>
    </template>
    <p v-if="error" class="error">{{ error }}</p>
    <p><a href="/">Open Zabbix Digest</a></p>
  </div>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { api } from "../api.js";

const props = defineProps({ token: { type: String, required: true } });

const info = ref(null);
const loading = ref(true);
const busy = ref(false);
const done = ref(false);
const error = ref("");

onMounted(async () => {
  try {
    info.value = await api.get(`/api/unsubscribe/${encodeURIComponent(props.token)}`);
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
});

async function confirm() {
  busy.value = true;
  error.value = "";
  try {
    await api.post(`/api/unsubscribe/${encodeURIComponent(props.token)}`);
    done.value = true;
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
