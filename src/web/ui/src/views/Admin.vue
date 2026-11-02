<template>
  <div>
    <div class="panel">
      <h2>Administration</h2>
      <p v-if="stats">
        Users: {{ stats.users }}, with active subscriptions: {{ stats.users_with_active }}.
        Subscriptions: {{ stats.subscriptions_enabled }} enabled of {{ stats.subscriptions }}.
      </p>
    </div>

    <div class="panel">
      <div class="list-head">
        <h3>Users</h3>
        <input v-model.trim="filter" type="text" placeholder="Filter by email" aria-label="Filter by email" />
        <select v-model="activity" aria-label="Filter by subscriptions">
          <option value="all">All</option>
          <option value="active">Active</option>
          <option value="inactive">Non-active</option>
        </select>
      </div>
      <p class="hint">Active: at least one enabled subscription.</p>
      <p v-if="loading" class="muted">Loading...</p>
      <template v-else-if="users">
        <p class="muted">Shown: {{ shown.length }} of {{ users.length }}.</p>
        <table class="list">
          <thead>
            <tr>
              <th>Email</th>
              <th>Zabbix user ID</th>
              <th>Subscriptions (enabled / total)</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="u in shown" :key="u.email">
              <td>{{ u.email }}</td>
              <td>{{ u.zabbix_userid || "-" }}</td>
              <td>{{ u.subscriptions_enabled }} / {{ u.subscriptions_total }}</td>
              <td>
                <button v-if="u.email !== me.email" :disabled="busy" @click="impersonate(u.email)">Impersonate</button>
              </td>
            </tr>
            <tr v-if="!shown.length">
              <td colspan="4" class="muted">No users match the filter.</td>
            </tr>
          </tbody>
        </table>
      </template>
      <p v-if="error" class="error">{{ error }}</p>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { api } from "../api.js";

defineProps({ me: { type: Object, required: true } });
const emit = defineEmits(["changed"]);

const users = ref(null);
const stats = ref(null);
const filter = ref("");
// all, active (at least one enabled subscription), inactive.
const activity = ref("all");
const loading = ref(true);
const busy = ref(false);
const error = ref("");

const shown = computed(() => {
  const text = filter.value.toLowerCase();
  return (users.value || []).filter((u) => {
    if (activity.value === "active" && !u.subscriptions_enabled) return false;
    if (activity.value === "inactive" && u.subscriptions_enabled) return false;
    return u.email.toLowerCase().includes(text);
  });
});

onMounted(async () => {
  try {
    const data = await api.get("/api/admin/users");
    users.value = data.users;
    stats.value = data.stats;
  } catch (e) {
    error.value = e.message;
  } finally {
    loading.value = false;
  }
});

async function impersonate(email) {
  busy.value = true;
  error.value = "";
  try {
    await api.post("/api/admin/impersonate", { email });
    emit("changed");
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
