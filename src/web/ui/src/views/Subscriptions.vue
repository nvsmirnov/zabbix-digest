<template>
  <div>
    <div class="panel">
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
        <h2 style="margin: 0">Subscriptions</h2>
        <button v-if="!creating" class="primary" :disabled="limitReached" @click="creating = true">
          New subscription
        </button>
      </div>
      <p class="muted">
        Daily digests of active problems for {{ me.email }}.
        <span v-if="me.max_subscriptions">Limit: {{ me.max_subscriptions }} subscriptions.</span>
      </p>
      <div v-if="creating" class="panel">
        <h3>New subscription</h3>
        <SubscriptionForm
          :transports="me.transports"
          :timezone="me.timezone"
          @saved="afterSave"
          @cancel="creating = false"
        />
      </div>
      <p v-if="!me.subscriptions.length && !creating" class="muted">No subscriptions yet.</p>
      <table v-if="me.subscriptions.length" class="list">
        <thead>
          <tr>
            <th>Subscription</th>
            <th>Time</th>
            <th>Delivery</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="s in me.subscriptions" :key="s.id">
            <tr :class="{ 'disabled-row': !s.enabled }">
              <td>
                {{ s.title }}
                <div v-if="filterNotes(s)" class="muted">{{ filterNotes(s) }}</div>
              </td>
              <td>{{ s.time }}</td>
              <td>{{ s.transport }}</td>
              <td class="status">{{ s.enabled ? "Enabled" : "Disabled" }}</td>
              <td>
                <div class="actions" style="margin-top: 0">
                  <button @click="toggleEdit(s.id)">Edit</button>
                  <button :disabled="busy" @click="setEnabled(s, !s.enabled)">{{ s.enabled ? "Disable" : "Enable" }}</button>
                  <button
                    :disabled="busy || !supportsPreview(s)"
                    :title="supportsPreview(s) ? '' : 'Preview is not available for this delivery method'"
                    @click="preview(s)"
                  >
                    Preview
                  </button>
                  <button :disabled="busy" title="Send the real digest now" @click="sendNow(s)">Send now</button>
                  <button :disabled="busy" title="Send a message with sample data" @click="testSend(s)">Send test</button>
                  <button class="danger" :disabled="busy" @click="remove(s)">Delete</button>
                </div>
              </td>
            </tr>
            <tr v-if="editing === s.id">
              <td colspan="5">
                <SubscriptionForm
                  :subscription="s"
                  :transports="me.transports"
                  :timezone="me.timezone"
                  @saved="afterSave"
                  @cancel="editing = null"
                />
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <p v-if="message" class="success">{{ message }}</p>
      <p v-if="error" class="error">{{ error }}</p>
    </div>

    <div v-if="previewData" class="panel">
      <div style="display: flex; justify-content: space-between; align-items: center">
        <h3 style="margin: 0">Preview: {{ previewData.subject }}</h3>
        <button @click="previewData = null">Close</button>
      </div>
      <p class="muted">Real data, rendered as the email would look. Nothing was sent.</p>
      <iframe class="preview" sandbox="allow-popups allow-popups-to-escape-sandbox" :srcdoc="previewData.body"></iframe>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";
import { api, SEVERITIES } from "../api.js";
import SubscriptionForm from "../components/SubscriptionForm.vue";

const props = defineProps({ me: { type: Object, required: true } });
const emit = defineEmits(["changed"]);

const creating = ref(false);
const editing = ref(null);
const busy = ref(false);
const message = ref("");
const error = ref("");
const previewData = ref(null);

const limitReached = computed(
  () => props.me.max_subscriptions && props.me.subscriptions.length >= props.me.max_subscriptions,
);

function supportsPreview(s) {
  const t = props.me.transports.find((x) => x.name === s.transport);
  return !!(t && t.preview);
}

function toggleEdit(id) {
  editing.value = editing.value === id ? null : id;
}

function afterSave() {
  creating.value = false;
  editing.value = null;
  message.value = "Saved.";
  error.value = "";
  emit("changed");
}

function filterNotes(s) {
  const notes = [];
  if (s.min_severity !== null && s.min_severity !== undefined) notes.push(`${SEVERITIES[s.min_severity]} and higher`);
  if (s.hide_acknowledged) notes.push("no acknowledged");
  if (s.hide_suppressed) notes.push("no suppressed");
  return notes.join(", ");
}

async function run(action, success) {
  busy.value = true;
  message.value = "";
  error.value = "";
  try {
    await action();
    if (success) message.value = success;
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}

function setEnabled(s, enabled) {
  run(async () => {
    await api.put(`/api/subscriptions/${s.id}`, {
      name: s.name,
      widget_url: s.widget_url,
      time: s.time,
      transport: s.transport,
      min_severity: s.min_severity,
      hide_acknowledged: s.hide_acknowledged,
      hide_suppressed: s.hide_suppressed,
      enabled,
    });
    emit("changed");
  }, enabled ? "Subscription enabled." : "Subscription disabled.");
}

function preview(s) {
  run(async () => {
    previewData.value = await api.post(`/api/subscriptions/${s.id}/preview`);
  });
}

function sendNow(s) {
  run(async () => {
    const r = await api.post(`/api/subscriptions/${s.id}/send-now`);
    if (r.result === "empty") {
      message.value = `No active problems for ${s.title}. Nothing was sent.`;
    } else {
      const total = `${r.total}${r.total_exact ? "" : "+"}`;
      message.value = `Digest sent for ${s.title}: ${r.shown} of ${total} problems.`;
    }
  });
}

function testSend(s) {
  run(() => api.post(`/api/subscriptions/${s.id}/test-send`), `Test message sent for ${s.title}.`);
}

function remove(s) {
  if (!window.confirm(`Delete subscription ${s.title}? This cannot be undone.`)) return;
  run(async () => {
    await api.del(`/api/subscriptions/${s.id}`);
    emit("changed");
  }, "Subscription deleted.");
}
</script>
