(() => {
  const $ = (selector) => document.querySelector(selector);
  const tokenKey = 'northstar-parent-admin-token';
  const state = { data: null, applications: [], selectedTutor: null, busy: false };
  const tutorTabs = $('#tutorTabs');
  const notice = $('#notice');
  const authDialog = $('#authDialog');

  function adminToken() { return sessionStorage.getItem(tokenKey) || ''; }
  function showNotice(message, kind = 'info') {
    notice.textContent = message;
    notice.className = `notice ${kind}`;
    notice.hidden = false;
    window.clearTimeout(showNotice.timer);
    showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 7000);
  }
  async function api(path, options = {}, auth = true) {
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body) headers.set('Content-Type', 'application/json');
    if (auth && adminToken()) headers.set('Authorization', `Bearer ${adminToken()}`);
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
    return data;
  }
  function escapeText(value) { return String(value ?? ''); }
  function setBusy(busy) {
    state.busy = busy;
    $('#evaluateButton').disabled = busy || !state.data?.candidate?.model;
    $('#evaluateButton').innerHTML = busy ? 'Checking behavior… <span>◌</span>' : 'Run compatibility check <span>↗</span>';
  }
  function render(stateData) {
    state.data = stateData;
    const active = stateData.active;
    const candidate = stateData.candidate;
    $('#activeModel').textContent = active.model;
    $('#activeRelease').textContent = `Azure OpenAI · ${active.release_id}`;
    $('#currentReleaseModel').textContent = `${active.model} · ${active.release_id}`;
    $('#candidateModel').value = candidate.model;
    $('#evaluateButton').disabled = state.busy || !candidate.model;
    $('#compareButton').disabled = !candidate.model;
    $('#contractEditor').value = candidate.behavior_contract;

    const ids = Object.keys(candidate.personas);
    if (!state.selectedTutor || !candidate.personas[state.selectedTutor]) state.selectedTutor = ids[0];
    tutorTabs.replaceChildren();
    ids.forEach((id) => {
      const persona = candidate.personas[id];
      const button = document.createElement('button');
      button.type = 'button'; button.className = `tutor-tab${id === state.selectedTutor ? ' active' : ''}`;
      button.setAttribute('role', 'tab'); button.setAttribute('aria-selected', String(id === state.selectedTutor));
      button.textContent = persona.short_name || persona.name;
      button.addEventListener('click', () => { state.selectedTutor = id; render(state.data); });
      tutorTabs.append(button);
    });
    const persona = candidate.personas[state.selectedTutor];
    $('#promptEditor').value = persona.system_prompt;
    $('#personaSummary').replaceChildren();
    const personaName = document.createElement('strong'); personaName.textContent = persona.name;
    const personaStyle = document.createElement('p'); personaStyle.textContent = `${persona.role || 'Tutor'} · ${persona.style || 'custom style'}`;
    $('#personaSummary').append(personaName, personaStyle);

    $('#historyList').replaceChildren();
    stateData.releases.forEach((release) => {
      const row = document.createElement('div'); row.className = 'history-item';
      const info = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = release.release_id;
      const sub = document.createElement('small'); sub.textContent = `Contract v${release.contract_revision} · prompts v${release.prompt_revision} · ${new Date(release.promoted_at || release.created_at).toLocaleString()}`;
      info.append(title, sub);
      const model = document.createElement('span'); model.className = 'history-model'; model.textContent = release.model;
      const tag = document.createElement('span'); tag.className = release.is_active ? 'history-active' : 'history-old'; tag.textContent = release.is_active ? 'ACTIVE' : 'PREVIOUS';
      row.append(info, model, tag); $('#historyList').append(row);
    });
    $('#rollbackButton').hidden = !stateData.can_rollback;
    renderEvaluation(stateData.last_evaluation, stateData.candidate_fingerprint);
  }
  function renderEvaluation(evaluation, fingerprint) {
    const summary = $('#evaluationSummary');
    const caseList = $('#caseList');
    caseList.replaceChildren();
    const label = $('#evaluationStatus');
    const gate = $('#gatePill');
    const promote = $('#promoteButton');
    const allowed = Boolean(evaluation && evaluation.promotion_allowed && evaluation.status === 'passed');
    const promoted = Boolean(evaluation && evaluation.status === 'passed' && state.data?.active?.evaluation_id === evaluation.evaluation_id);
    promote.disabled = !allowed;
    gate.className = `gate-pill${allowed ? ' pass' : evaluation && ['failed', 'blocked', 'stale'].includes(evaluation.status) ? ' fail' : ''}`;
    gate.querySelector('span').textContent = allowed ? 'All configured checks passed' : promoted ? 'Candidate promoted' : evaluation?.status === 'running' ? 'Compatibility check running' : evaluation && ['failed', 'blocked', 'stale'].includes(evaluation.status) ? 'Promotion blocked' : 'Checks required';
    if (!evaluation) {
      label.className = 'status-label'; label.textContent = 'AWAITING RUN';
      summary.className = 'evaluation-summary';
      summary.innerHTML = stateDataCandidateMessage();
      return;
    }
    const status = promoted ? 'promoted' : evaluation.promotion_allowed ? 'passed' : evaluation.status;
    label.className = `status-label ${status}`; label.textContent = String(status).replaceAll('_', ' ').toUpperCase();
    summary.className = 'evaluation-summary';
    const icon = document.createElement('span'); icon.className = 'empty-icon'; icon.textContent = evaluation.promotion_allowed ? '✓' : '×';
    if (status === 'running') icon.textContent = '...';
    const title = document.createElement('strong'); title.textContent = promoted ? `Compatibility passed; ${state.data.active.model} is active` : evaluation.promotion_allowed ? 'Compatibility checks passed' : status === 'running' ? 'Checking candidate behavior' : `Candidate ${status}`;
    const desc = document.createElement('p'); desc.textContent = evaluation.summary || 'No evaluation summary was returned.';
    summary.replaceChildren(icon, title, desc);
    (evaluation.cases || []).forEach((item) => {
      const card = document.createElement('article'); card.className = 'case-card';
      const head = document.createElement('div'); head.className = 'case-head';
      const caseTitle = document.createElement('strong'); caseTitle.textContent = `${item.application_id || item.tutor_id || 'Client app'} · ${item.case_id || 'Compatibility case'}`;
      const caseState = document.createElement('span'); caseState.className = `case-state ${item.status || 'blocked'}`; caseState.textContent = item.status || 'blocked';
      head.append(caseTitle, caseState); card.append(head);
      const meta = document.createElement('div'); meta.className = 'case-meta';
      const candidateScores = item.candidate_scores || {};
      meta.textContent = item.error || Object.entries(candidateScores).map(([key, value]) => `${key.replaceAll('_', ' ')} ${value}/5`).join(' · ') || 'No scores were recorded'; card.append(meta);
      const issues = [...(item.candidate_check?.issues || []), ...(item.judge_issues || [])];
      if (issues.length) { const p = document.createElement('p'); p.className = 'case-issues'; p.textContent = [...new Set(issues)].join(' · '); card.append(p); }
      if (item.candidate_answer) {
        const details = document.createElement('details'); details.className = 'case-answer';
        const summaryLine = document.createElement('summary'); summaryLine.textContent = 'Review baseline and candidate answers'; details.append(summaryLine);
        [['Candidate answer', item.candidate_answer], ['Active answer', item.baseline_answer]].forEach(([labelText, answer]) => {
          const heading = document.createElement('strong'); heading.textContent = labelText;
          const pre = document.createElement('pre'); pre.textContent = escapeText(answer); details.append(heading, pre);
        }); card.append(details);
      }
      caseList.append(card);
    });
  }
  function stateDataCandidateMessage() {
    const hasCandidate = Boolean(state.data?.candidate?.model);
    return hasCandidate
      ? '<span class="empty-icon">⌁</span><strong>Ready when you are</strong><p>Run the compatibility check to compare this deployment with the active release.</p>'
      : '<span class="empty-icon">⌁</span><strong>No candidate deployment staged</strong><p>Set TUTOR_MODEL to the candidate Azure deployment and restart the parent. It will be evaluated while the saved active release keeps serving; review results before promotion.</p>';
  }
  function renderApplications(applications) {
    state.applications = applications;
    const grid = $('#applicationGrid');
    grid.replaceChildren();
    const online = applications.filter((item) => item.status === 'online').length;
    $('#applicationHealthCount').textContent = `${online}/${applications.length}`;
    $('#applicationRegistryStatus').textContent = applications.length ? `${online} OF ${applications.length} ONLINE` : 'NO APPS';
    const caseCount = applications.reduce((sum, item) => sum + Number(item.benchmark_cases || 0), 0);
    $('#regressionCaseCount').textContent = `${caseCount} cases`;
    if (!applications.length) {
      const empty = document.createElement('div'); empty.className = 'application-empty'; empty.textContent = 'No client applications are registered.'; grid.append(empty); return;
    }
    const compareSelect = $('#compareApplication');
    const selectedApplication = compareSelect.value;
    compareSelect.replaceChildren();
    applications.forEach((item) => {
      const option = document.createElement('option'); option.value = item.application_id;
      option.textContent = item.display_name || item.application_id; compareSelect.append(option);
    });
    if (applications.some((item) => item.application_id === selectedApplication)) compareSelect.value = selectedApplication;
    applications.forEach((item) => {
      const card = document.createElement('article'); card.className = 'application-card';
      const head = document.createElement('div'); head.className = 'application-card-head';
      const nameWrap = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = item.display_name || item.application_id;
      const id = document.createElement('span'); id.className = 'application-id'; id.textContent = item.application_id;
      nameWrap.append(name, id);
      const status = document.createElement('span'); status.className = `application-state ${item.status || 'offline'}`;
      const dot = document.createElement('i'); const statusText = document.createElement('span'); statusText.textContent = item.status || 'offline'; status.append(dot, statusText);
      head.append(nameWrap, status); card.append(head);
      const meta = document.createElement('div'); meta.className = 'application-meta';
      const addMeta = (labelText, value, wide = false) => {
        const cell = document.createElement('div'); if (wide) cell.className = 'wide';
        const label = document.createElement('small'); label.textContent = labelText;
        const content = document.createElement('strong'); content.textContent = value || 'Unavailable';
        cell.append(label, content); meta.append(cell);
      };
      addMeta('ACTIVE MODEL', item.observed_model || item.active_model);
      addMeta('REGRESSION CASES', String(item.benchmark_cases ?? 0));
      addMeta('DATA SOURCE', item.database && item.collection ? `${item.database}.${item.collection} | ${item.student_count ?? 0} records` : item.data_source || 'Client-supplied context', true);
      addMeta('PROMPT SNAPSHOT', item.prompt_sha256 ? `${item.prompt_sha256.slice(0, 12)} | v${item.prompt_revision}${item.prompt_matches_snapshot ? '' : ' | MISMATCH'}` : 'Not loaded', true);
      addMeta('CANDIDATE', item.candidate_model ? `${item.candidate_model} | ${item.candidate_status}` : 'None staged', true);
      addMeta('LATEST EVALUATION', item.latest_evaluation_status ? `${item.latest_evaluation_status}${item.latest_evaluation_at ? ` | ${new Date(item.latest_evaluation_at).toLocaleString()}` : ''}` : 'Not run', true);
      addMeta('API CONTRACT', item.request_contract, true);
      const toolCount = Array.isArray(item.tool_schema) ? item.tool_schema.length : item.tool_schema && typeof item.tool_schema === 'object' ? Object.keys(item.tool_schema).length : 0;
      addMeta('TOOLS', String(toolCount));
      addMeta('DATA SCOPE', item.application_data_scope || item.data_source, true);
      addMeta('AUTH', item.auth_mode, true);
      card.append(meta); grid.append(card);
    });
  }
  async function loadApplications() {
    try {
      const result = await api('/api/control/applications');
      renderApplications(result.applications || []);
    } catch (error) {
      $('#applicationRegistryStatus').textContent = 'UNAVAILABLE';
      const grid = $('#applicationGrid'); grid.replaceChildren();
      const empty = document.createElement('div'); empty.className = 'application-empty'; empty.textContent = error.message; grid.append(empty);
    }
  }
  async function loadState() {
    try {
      const data = await api('/api/control/state');
      render(data);
      await loadApplications();
      return true;
    } catch (error) {
      if (/token|unauthorized|control/i.test(error.message) && !adminToken()) {
        authDialog.showModal();
        showNotice('Connect with the parent admin token to view release details and manage candidates.', 'info');
      } else showNotice(error.message, 'error');
      return false;
    }
  }
  function authHeaderBody(value) { return JSON.stringify(value); }

  $('#authOpen').addEventListener('click', () => authDialog.showModal());
  $('#authForm').addEventListener('submit', async (event) => {
    if (event.submitter?.value === 'cancel') return;
    event.preventDefault();
    const token = $('#adminToken').value.trim();
    if (!token) return;
    sessionStorage.setItem(tokenKey, token);
    $('#adminToken').value = '';
    try {
      await api('/api/control/state');
      authDialog.close(); showNotice('Connected to the parent control plane.', 'success'); await loadState();
    } catch (error) {
      sessionStorage.removeItem(tokenKey); showNotice(error.message, 'error');
    }
  });
  $('#refreshButton').addEventListener('click', loadState);
  $('#registerApplicationOpen').addEventListener('click', () => $('#registerApplicationDialog').showModal());
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => {
    const dialog = document.getElementById(button.dataset.close); if (dialog?.open) dialog.close();
  }));
  $('#registerApplicationForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    let regressionCases;
    try { regressionCases = JSON.parse($('#newApplicationCases').value); }
    catch { showNotice('Regression cases must be valid JSON.', 'error'); return; }
    if (!Array.isArray(regressionCases)) { showNotice('Regression cases must be a JSON array.', 'error'); return; }
    let apiSchema;
    let toolSchema;
    let dataIntegration;
    try {
      apiSchema = $('#newApplicationApiSchema').value.trim() ? JSON.parse($('#newApplicationApiSchema').value) : undefined;
      toolSchema = $('#newApplicationToolSchema').value.trim() ? JSON.parse($('#newApplicationToolSchema').value) : undefined;
      dataIntegration = $('#newApplicationDataIntegration').value.trim() ? JSON.parse($('#newApplicationDataIntegration').value) : undefined;
    } catch { showNotice('Data integration, API, and tool schemas must be valid JSON.', 'error'); return; }
    const payload = {
      application_id: $('#newApplicationId').value.trim(),
      display_name: $('#newApplicationName').value.trim(),
      role: $('#newApplicationRole').value.trim(),
      style: $('#newApplicationStyle').value.trim(),
      system_prompt: $('#newApplicationPrompt').value,
      data_source: $('#newApplicationData').value.trim(),
      ...(dataIntegration === undefined ? {} : { data_integration: dataIntegration }),
      ...(apiSchema === undefined ? {} : { api_schema: apiSchema }),
      ...(toolSchema === undefined ? {} : { tool_schema: toolSchema }),
      health_url: $('#newApplicationHealth').value.trim(),
      regression_cases: regressionCases,
    };
    const submit = event.submitter; if (submit) submit.disabled = true;
    try {
      const result = await api('/api/control/applications', { method: 'POST', body: JSON.stringify(payload) });
      $('#createdCredential').value = result.gateway_token || '';
      $('#registerApplicationDialog').close(); $('#registerApplicationForm').reset();
      $('#credentialDialog').showModal();
      await loadState();
      showNotice(`${result.application?.display_name || payload.display_name} is registered. Copy its token before closing this dialog.`, 'success');
    } catch (error) { showNotice(error.message, 'error'); }
    finally { if (submit) submit.disabled = false; }
  });
  $('#copyCredential').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#createdCredential').value); showNotice('Application token copied.', 'success'); }
    catch { $('#createdCredential').select(); document.execCommand('copy'); showNotice('Application token copied.', 'success'); }
  });
  $('#compareForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    let studentRecords;
    try { studentRecords = JSON.parse($('#compareRecords').value); }
    catch { showNotice('Student records must be valid JSON.', 'error'); return; }
    if (!Array.isArray(studentRecords)) { showNotice('Student records must be a JSON array.', 'error'); return; }
    const button = $('#compareButton'); button.disabled = true; button.textContent = 'Generating both answers…';
    const panel = $('#comparisonResult'); panel.hidden = false; panel.textContent = 'Comparing with the same app prompt and student records…';
    try {
      const result = await api('/api/control/compare', { method: 'POST', body: JSON.stringify({
        application_id: $('#compareApplication').value,
        question: $('#compareQuestion').value,
        student_records: studentRecords,
        required_facts: $('#compareFacts').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean),
      }) });
      panel.replaceChildren();
      const heading = document.createElement('strong'); heading.textContent = `${result.active_model} compared with ${result.candidate_model}`; panel.append(heading);
      const hash = document.createElement('small'); hash.textContent = `Registered prompt ${String(result.prompt_sha256 || '').slice(0, 16)}`; panel.append(hash);
      const columns = document.createElement('div'); columns.className = 'comparison-columns';
      [['Active answer', result.baseline_answer, result.baseline_check], ['Candidate answer', result.candidate_answer, result.candidate_check]].forEach(([titleText, answer, check]) => {
        const article = document.createElement('article'); article.className = 'comparison-answer';
        const title = document.createElement('h3'); title.textContent = titleText; article.append(title);
        const text = document.createElement('pre'); text.textContent = answer || ''; article.append(text);
        const status = document.createElement('small'); status.className = check?.passed ? 'check-pass' : 'check-fail';
        status.textContent = check?.passed ? 'Deterministic checks passed' : (check?.issues || ['Deterministic checks need review']).join(' · '); article.append(status);
        columns.append(article);
      });
      panel.append(columns);
    } catch (error) { panel.textContent = error.message; showNotice(error.message, 'error'); }
    finally { button.disabled = !state.data?.candidate?.model; button.textContent = 'Compare active and candidate'; }
  });
  $('#candidateModel').addEventListener('change', async (event) => {
    try {
      await api('/api/control/candidate/model', { method: 'PUT', body: authHeaderBody({ model: event.target.value.trim() }) });
      await loadState(); showNotice(event.target.value.trim() ? 'Candidate model saved. Run the checks before promotion.' : 'Candidate model cleared.', 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });
  $('#evaluateButton').addEventListener('click', async () => {
    if (!adminToken()) { authDialog.showModal(); return; }
    try {
      const savedModel = $('#candidateModel').value.trim();
      if (!savedModel) { showNotice('Enter a candidate Azure deployment name first.', 'error'); return; }
      if (savedModel !== state.data?.candidate?.model) {
        await api('/api/control/candidate/model', { method: 'PUT', body: authHeaderBody({ model: savedModel }) });
      }
      setBusy(true);
      $('#evaluationSummary').classList.add('running');
      $('#evaluationSummary').innerHTML = '<span class="empty-icon">◌</span><strong>Running behavior checks</strong><p>Azure is generating active and candidate answers for each registered app, then evaluating the differences. This can take a little while.</p>';
      const result = await api('/api/control/evaluate', { method: 'POST' });
      await loadState();
      showNotice(result.summary || 'Compatibility check finished.', result.status === 'passed' ? 'success' : 'error');
    } catch (error) { showNotice(error.message, 'error'); }
    finally { setBusy(false); }
  });
  $('#saveContractButton').addEventListener('click', async () => {
    try {
      await api('/api/control/candidate/contract', { method: 'PUT', body: authHeaderBody({ behavior_contract: $('#contractEditor').value }) });
      await loadState(); showNotice('Shared contract saved to the candidate only. Run the checks before promotion.', 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });
  $('#promoteButton').addEventListener('click', async () => {
    if (!state.data?.last_evaluation?.promotion_allowed) return;
    if (!window.confirm(`Have you reviewed the active and candidate answers for the registered applications? Promote ${state.data.candidate.model} only if its behavior, facts, and API contract meet your requirements.`)) return;
    try {
      const result = await api('/api/control/promote', { method: 'POST' });
      render(result.state); showNotice(`${result.release.release_id} is now active on ${result.release.model}.`, 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });
  $('#rollbackButton').addEventListener('click', async () => {
    if (!window.confirm('Roll back tutor traffic to the previous active release?')) return;
    try {
      const result = await api('/api/control/rollback', { method: 'POST' });
      render(result.state); showNotice(`Tutor traffic rolled back to ${result.release.release_id} · ${result.release.model}.`, 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });

  const startupRefreshUntil = Date.now() + 45000;
  window.setInterval(() => {
    const isChecking = state.data?.last_evaluation?.status === 'running';
    if (adminToken() && (isChecking || Date.now() < startupRefreshUntil)) loadState();
  }, 3000);
  loadState();
})();
