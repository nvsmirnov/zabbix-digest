<template>
  <form @submit.prevent="save">
    <label :for="`name-${uid}`">Name (optional)</label>
    <input :id="`name-${uid}`" v-model="form.name" type="text" maxlength="200" />

    <label :for="`url-${uid}`">Dashboard link (the first Problems widget is used)</label>
    <input :id="`url-${uid}`" v-model.trim="form.widget_url" type="url" required />
    <p class="hint">
      Open the dashboard in Zabbix and copy its address. The digest uses the first Problems widget of the dashboard
      (first page, top-left). Filters, sorting and the line limit come from the widget settings.
    </p>

    <label :for="`severity-${uid}`">Minimal severity</label>
    <select :id="`severity-${uid}`" v-model="form.min_severity">
      <option :value="null">Widget setting</option>
      <option v-for="(name, value) in SEVERITIES" :key="value" :value="value">{{ name }}</option>
    </select>
    <p class="hint">Narrows the widget severity filter: only problems of this severity and higher are included.</p>

    <label style="font-weight: normal">
      <input v-model="form.hide_acknowledged" type="checkbox" />
      Hide acknowledged problems
    </label>
    <label style="font-weight: normal">
      <input v-model="form.hide_suppressed" type="checkbox" />
      Hide suppressed problems
    </label>
    <p class="hint">When not checked, the widget setting is used. These options cannot show problems the widget hides.</p>

    <label :for="`time-${uid}`">Daily delivery time</label>
    <input :id="`time-${uid}`" v-model="form.time" type="time" required />
    <p class="hint">Time zone of the service: {{ timezone || "unknown" }}.</p>

    <label :for="`transport-${uid}`">Delivery method</label>
    <select :id="`transport-${uid}`" v-model="form.transport">
      <option v-if="form.transport && !names.includes(form.transport)" :value="form.transport">
        {{ form.transport }} (set by administrator)
      </option>
      <option v-for="t in transports" :key="t.name" :value="t.name">{{ t.name }}</option>
    </select>

    <label style="font-weight: normal">
      <input v-model="form.enabled" type="checkbox" />
      Enabled
    </label>

    <div class="actions">
      <button class="primary" type="submit" :disabled="busy">Save</button>
      <button type="button" @click="emit('cancel')">Cancel</button>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
  </form>
</template>

<script setup>
import { computed, reactive, ref } from "vue";
import { api, SEVERITIES } from "../api.js";

const props = defineProps({
  subscription: { type: Object, default: null },
  transports: { type: Array, required: true },
  timezone: { type: String, default: "" },
});
const emit = defineEmits(["saved", "cancel"]);

const uid = Math.random().toString(16).slice(2, 8);
const source = props.subscription || {};
const form = reactive({
  name: source.name || "",
  widget_url: source.widget_url || "",
  time: source.time || "08:00",
  transport: source.transport || (props.transports[0] && props.transports[0].name) || "email",
  enabled: source.enabled !== undefined ? source.enabled : true,
  min_severity: source.min_severity ?? null,
  hide_acknowledged: source.hide_acknowledged || false,
  hide_suppressed: source.hide_suppressed || false,
});
const names = computed(() => props.transports.map((t) => t.name));
const busy = ref(false);
const error = ref("");

// On failure the form keeps the entered values, so the user can press Save again.
async function save() {
  busy.value = true;
  error.value = "";
  try {
    const body = { ...form, name: form.name.trim() || null };
    const result = props.subscription
      ? await api.put(`/api/subscriptions/${props.subscription.id}`, body)
      : await api.post("/api/subscriptions", body);
    emit("saved", result);
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>
