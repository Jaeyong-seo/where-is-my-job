/* Deterministic Aside REPL fixture. It never navigates or contacts a provider. */
const APPLICATION_AUTOMATION_REQUEST = /* APPLICATION_AUTOMATION_REQUEST */ null;
const input = APPLICATION_AUTOMATION_REQUEST;
const operation = input.operation;
const {
  scenario, run_key, provider, tenant, account_id_hmac, context_id_hmac, session_id_hmac,
  fixture_phase, dispatch_id, application_id, session_id, run_id, intent_hmac, payload_sha256,
  page_fingerprint, form_fingerprint, resume_sha256, field_digest
} = input.input;
const pauses = {
  captcha: "captcha", mfa: "mfa", security_challenge: "security_challenge",
  rate_limit: "rate_limit", provider_challenge: "provider_challenge", login: "login",
  account_creation: "account_creation", new_question: "new_question",
  unknown_question: "unknown_question", sensitive_question: "sensitive_question",
  legal_question: "legal_question", required_demographics: "required_demographics",
  street_address: "street_address", salary_unverified: "salary_unverified",
  salary_exact_number: "salary_exact_number", attestation: "attestation",
  form_drift: "form_drift", posting_drift: "posting_drift",
  unexpected_redirect: "unexpected_redirect"
};
const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
if (typeof scenario !== "string" || (!Object.hasOwn(pauses, scenario) && scenario !== "happy" && scenario !== "ambiguous")) {
  throw new Error("unknown fixture scenario");
}
if (![run_key, provider, tenant].every(value => typeof value === "string" && value) || ![account_id_hmac, context_id_hmac, session_id_hmac].every(digest)) {
  throw new Error("fixture context identity is incomplete");
}
if (!["inspect", "fill", "submit", "observe"].includes(operation)) {
  throw new Error("unknown fixture operation");
}
if (!["new", "inspected", "filled", "started"].includes(fixture_phase)) {
  throw new Error("fixture lifecycle state is invalid");
}
if (operation !== "inspect" && fixture_phase === "new") {
  throw new Error("operation requires a run inspection");
}
if (operation === "observe" && (typeof dispatch_id !== "string" || !dispatch_id)) {
  throw new Error("observe requires a dispatch ID");
}
const paused = pauses[scenario];
const fingerprints = paused === "form_drift"
  ? ["fixture-page-v1", "fixture-form-drift"]
  : paused === "posting_drift"
    ? ["fixture-posting-drift", "fixture-form-v1"]
    : paused === "unexpected_redirect"
      ? ["fixture-page-drift", "fixture-form-v1"]
      : ["fixture-page-v1", "fixture-form-v1"];
const result = {
  schema: "application_automation.aside.v1", operation,
  domain: "fixture.local", page_fingerprint: fingerprints[0], form_fingerprint: fingerprints[1]
};
if (paused) result.pause_reason = paused;
if (operation === "inspect") {
  if (fixture_phase !== "new") throw new Error("fixture run key is already in use");
  result.fields = ["name", "email", "resume"];
} else if (operation === "fill") {
  if (!paused && fixture_phase !== "inspected") throw new Error("fill requires a successful inspection");
  Object.assign(result, { filled: !paused, attached_resume_sha256: paused ? null : (resume_sha256 ?? null) });
} else if (operation === "submit") {
  if (![dispatch_id, application_id, session_id, run_id].every(value => typeof value === "string" && value) || !digest(intent_hmac) || !digest(payload_sha256)) {
    throw new Error("dispatch identity is incomplete");
  }
  if (page_fingerprint !== "fixture-page-v1" || form_fingerprint !== "fixture-form-v1") {
    throw new Error("dispatch fingerprint mismatch");
  }
  if (resume_sha256 !== undefined && resume_sha256 !== null && !digest(resume_sha256)) {
    throw new Error("dispatch resume hash is invalid");
  }
  if (field_digest !== undefined && field_digest !== null && !digest(field_digest)) {
    throw new Error("dispatch field digest is invalid");
  }
  if (fixture_phase === "started") throw new Error("a started dispatch is never retried");
  if (!paused && fixture_phase !== "filled") throw new Error("submit requires a completed fill");
  Object.assign(result, paused ? {
    started: false, confirmed: false, manual_follow_up: false, receipt_id: null
  } : scenario === "ambiguous" ? {
    started: true, confirmed: false, manual_follow_up: true, receipt_id: null
  } : {
    started: true, confirmed: true, manual_follow_up: false, receipt_id: "fixture-receipt-v1"
  });
} else {
  Object.assign(result, paused ? {
    state: "awaiting_user", receipt_id: null
  } : fixture_phase === "started" && scenario === "happy" ? {
    state: "confirmed", receipt_id: "fixture-receipt-v1"
  } : fixture_phase === "started" ? {
    state: "manual_follow_up", receipt_id: null
  } : {
    state: "not_started", receipt_id: null
  });
}
console.log("APPLICATION_AUTOMATION_RESULT:" + JSON.stringify(result));
