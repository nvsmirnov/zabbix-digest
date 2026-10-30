<template>
  <Login v-if="needToken" :notice="error" @done="emit('done')" />
  <div v-else class="panel">
    <h2>Log in</h2>
    <p>Press the button to complete the login. The link can be used once.</p>
    <div class="actions">
      <button class="primary" :disabled="busy" @click="login">Log in</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
  </div>
</template>

<script setup>
import { ref } from "vue";
import { api } from "../api.js";
import Login from "./Login.vue";

const props = defineProps({ token: { type: String, required: true } });
const emit = defineEmits(["done"]);

const busy = ref(false);
const error = ref("");
const needToken = ref(false);

// The link is consumed only on explicit click, so mail scanners that open links do not use it.
async function login() {
  busy.value = true;
  error.value = "";
  try {
    await api.post("/api/auth/link", { token: props.token });
    emit("done");
  } catch (e) {
    error.value = e.message;
    if (e.code === "token_required") {
      needToken.value = true;
    }
  } finally {
    busy.value = false;
  }
}
</script>
