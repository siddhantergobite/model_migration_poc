(() => {
  const $ = (selector) => document.querySelector(selector);
  const tokenKey = 'northstar-parent-admin-token';
  const state = { data: null, applications: [], caseCount: 0, coreCaseCount: 0, holdoutCaseCount: 0, repetitions: 3, judgeModels: [], judgePanelConfigured: false, busy: false };
  const authDialog = $('#authDialog');
  const resultPanel = $('#comparisonResult');

  function adminToken() { return sessionStorage.getItem(tokenKey) || ''; }

  function showNotice(message, kind) {
    const notice = $('#notice');
    notice.textContent = message;
    notice.className = 'notice ' + (kind || 'info');
    notice.hidden = false;
    window.clearTimeout(showNotice.timer);
    showNotice.timer = window.setTimeout(() => { notice.hidden = true; }, 7000);
  }

  function selectedApplication() {
    return state.applications.find((item) => item.application_id === $('#compareApplication').value) || null;
  }

  function evaluateButtonLabel() {
    const total = state.coreCaseCount || 6;
    return 'Run ' + total + ' core checks (' + state.repetitions + ' runs each)';
  }

  function finalEvaluationButtonLabel() {
    const total = state.caseCount || 12;
    return 'Run final validation (' + total + ' cases)';
  }

  function renderCompatibilityGuidance() {
    const application = selectedApplication();
    const model = $('#candidateModel').value.trim();
    const personaId = application && (application.persona_id || application.application_id);
    const candidate = state.data && state.data.candidate;
    const adapter = candidate && candidate.model_adapters && candidate.model_adapters[model];
    const guidance = personaId && adapter ? adapter[personaId] || '' : '';
    if (document.activeElement !== $('#compatibilityGuidance')) {
      $('#compatibilityGuidance').value = guidance;
    }
    $('#saveCompatibilityGuidance').disabled = state.busy || !application || !model;
  }

  async function api(path, options, auth) {
    const settings = options || {};
    const headers = new Headers(settings.headers || {});
    headers.set('Accept', 'application/json');
    if (settings.body) headers.set('Content-Type', 'application/json');
    if (auth !== false && adminToken()) headers.set('Authorization', 'Bearer ' + adminToken());
    const response = await fetch(path, { ...settings, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(data.error || 'Request failed (HTTP ' + response.status + ').');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function updateButtons() {
    const hasCandidate = Boolean($('#candidateModel').value.trim());
    const hasTutor = Boolean($('#compareApplication').value);
    const hasQuestion = Boolean($('#compareQuestion').value.trim());
    const evaluationRunning = Boolean(state.data && state.data.last_evaluation && state.data.last_evaluation.status === 'running');
    $('#evaluateButton').disabled = state.busy || !hasCandidate || evaluationRunning;
    $('#finalEvaluateButton').disabled = state.busy || !hasCandidate || evaluationRunning || !state.holdoutCaseCount;
    $('#compareButton').disabled = state.busy || !hasCandidate || !hasTutor || !hasQuestion;
    const savedCandidate = state.data && state.data.candidate && state.data.candidate.model;
    const currentCandidate = $('#candidateModel').value.trim();
    const evaluation = state.data && state.data.last_evaluation;
    const canPromote = Boolean(
      !state.busy && savedCandidate && currentCandidate === savedCandidate
      && evaluation && evaluation.candidate_model === savedCandidate
      && evaluation.status === 'passed' && evaluation.promotion_allowed
    );
    $('#promoteButton').disabled = !canPromote;
    if ($('#saveCompatibilityGuidance')) renderCompatibilityGuidance();
  }

  function setBusy(busy, action) {
    state.busy = busy;
    $('#evaluateButton').textContent = busy && action === 'evaluate-core' ? 'Running core checks...' : evaluateButtonLabel();
    $('#finalEvaluateButton').textContent = busy && action === 'evaluate-final' ? 'Running final validation...' : finalEvaluationButtonLabel();
    $('#compareButton').textContent = busy && action === 'compare' ? 'Generating both answers...' : 'Compare active and candidate';
    updateButtons();
  }

  function render(stateData) {
    state.data = stateData;
    const active = stateData.active || {};
    const candidate = stateData.candidate || {};
    $('#activeModel').textContent = active.model || 'Unavailable';
    $('#activeRelease').textContent = active.release_id ? 'Release ' + active.release_id + ' is serving tutor requests.' : '';
    if (document.activeElement !== $('#candidateModel')) $('#candidateModel').value = candidate.model || '';
    $('#candidateModel').disabled = false;
    $('#candidateOverview').textContent = candidate.model || 'Not staged';

    const evaluation = stateData.last_evaluation;
    const evaluationMatchesCandidate = evaluation && evaluation.candidate_model === candidate.model;
    $('#candidateStatus').textContent = !candidate.model
      ? 'Enter a deployment below'
      : evaluationMatchesCandidate && evaluation.status === 'running'
        ? (evaluation.suite_stage === 'final' ? 'Final validation is running' : 'Core review is running')
      : evaluationMatchesCandidate && evaluation.status === 'passed' && evaluation.suite_stage !== 'final'
          ? 'Core review passed · final validation required'
          : evaluationMatchesCandidate && evaluation.status === 'passed' && evaluation.suite_stage === 'final'
            ? 'Final validation passed'
            : evaluationMatchesCandidate
              ? 'Latest ' + String(evaluation.suite_stage || 'core') + ' check: ' + String(evaluation.status || 'not run').replaceAll('_', ' ')
              : 'Staged; checks have not passed';

    $('#rollbackButton').hidden = !stateData.can_rollback;
    renderEvaluation(evaluation);
    renderCompatibilityGuidance();
    updateButtons();
  }

  function renderEvaluation(evaluation) {
    const summary = $('#evaluationSummary');
    const caseList = $('#caseList');
    const label = $('#evaluationStatus');
    const gate = $('#gatePill');
    const promote = $('#promoteButton');
    caseList.replaceChildren();

    const candidateModel = state.data && state.data.candidate && state.data.candidate.model;
    const inputModel = $('#candidateModel').value.trim();
    const allowed = Boolean(evaluation && evaluation.promotion_allowed && evaluation.status === 'passed' && evaluation.candidate_model === candidateModel && inputModel === candidateModel);
    const promoted = Boolean(evaluation && evaluation.status === 'passed' && state.data && state.data.active.evaluation_id === evaluation.evaluation_id);
    promote.disabled = !allowed || state.busy;
    gate.className = 'gate-pill' + (allowed ? ' pass' : evaluation && ['failed', 'blocked', 'stale'].includes(evaluation.status) ? ' fail' : '');
    gate.querySelector('span').textContent = allowed ? 'Final validation passed' : promoted ? 'Candidate is active'
      : evaluation && evaluation.status === 'running' ? 'Checks are running'
        : evaluation && evaluation.status === 'passed' && evaluation.suite_stage !== 'final' ? 'Final validation required'
          : evaluation && ['failed', 'blocked', 'stale'].includes(evaluation.status) ? 'Promotion blocked'
            : 'Checks required';

    if (!evaluation) {
      label.className = 'status-label';
      label.textContent = 'NOT RUN';
      summary.replaceChildren();
      const title = document.createElement('strong');
      title.textContent = 'Ready to evaluate';
      const description = document.createElement('p');
      description.textContent = 'Run core checks while refining guidance. Run final validation when the guidance is ready; a core review cannot promote a model.';
      summary.append(title, description);
      return;
    }

    const status = promoted ? 'promoted' : evaluation.status || 'blocked';
    label.className = 'status-label ' + status;
    label.textContent = status.replaceAll('_', ' ').toUpperCase();
    summary.replaceChildren();
    const title = document.createElement('strong');
    const coreReview = evaluation.suite_stage !== 'final';
    title.textContent = promoted ? state.data.active.model + ' is active'
      : status === 'running' ? (coreReview ? 'Running core review' : 'Running final validation')
        : status === 'passed' ? (coreReview ? 'Core review passed' : 'Final validation passed')
          : 'Candidate ' + status;
    const description = document.createElement('p');
    description.textContent = evaluation.summary || 'No evaluation summary was returned.';
    const stageNote = document.createElement('p');
    stageNote.className = 'case-meta';
    stageNote.textContent = coreReview
      ? 'Core review · not eligible for promotion'
      : 'Final validation · includes reserved cases';
    const firstPass = evaluation.first_pass_summary;
    if (firstPass && firstPass.active && firstPass.candidate) {
      const metric = document.createElement('p');
      metric.className = 'case-meta';
      metric.textContent = 'First-pass checks: active ' + firstPass.active.passed_runs + '/' + firstPass.active.total_runs
        + ', candidate ' + firstPass.candidate.passed_runs + '/' + firstPass.candidate.total_runs
        + ' · repairs: active ' + firstPass.active.repair_attempts + ', candidate ' + firstPass.candidate.repair_attempts;
      summary.append(title, description, stageNote, metric);
    } else {
      summary.append(title, description, stageNote);
    }
    const judgeModels = evaluation.judge_models || state.judgeModels;
    const judgeNote = document.createElement('p');
    judgeNote.className = 'case-meta';
    const panelConfigured = evaluation.judge_panel_configured ?? state.judgePanelConfigured;
    judgeNote.textContent = judgeModels.length && panelConfigured
      ? 'LLM judge panel: ' + judgeModels.join(', ') + ' · every judge must pass both blind answer orders'
      : 'LLM judge panel not configured · promotion is blocked';
    if (coreReview) {
      judgeNote.textContent = 'Core review uses deterministic checks only; LLM judges run during final validation.';
    }
    summary.append(judgeNote);
    (evaluation.judge_warnings || []).forEach((warning) => {
      const warningLine = document.createElement('p');
      warningLine.className = 'case-issues';
      warningLine.textContent = 'Judge independence warning: ' + warning;
      summary.append(warningLine);
    });

    (evaluation.cases || []).forEach((item) => {
      const card = document.createElement('article');
      card.className = 'case-card';
      const header = document.createElement('div');
      header.className = 'case-head';
      const name = document.createElement('strong');
      name.textContent = (item.case_group === 'holdout' ? 'Reserved validation · ' : '')
        + (item.tutor_id || item.application_id || 'Tutor') + ' / ' + (item.case_id || 'behavior case');
      const badge = document.createElement('span');
      badge.className = 'case-state ' + (item.status || 'blocked');
      badge.textContent = item.status || 'blocked';
      header.append(name, badge);
      card.append(header);

      const scoreText = [];
      const candidateScores = item.candidate_scores || {};
      Object.entries(candidateScores).forEach(([key, value]) => scoreText.push(key.replaceAll('_', ' ') + ' ' + value + '/5'));
      const meta = document.createElement('p');
      meta.className = 'case-meta';
      meta.textContent = item.error || scoreText.join(' | ') || item.judge_summary || 'No scores were recorded.';
      card.append(meta);
      if (item.judges && item.judges.length) {
        const judgeLine = document.createElement('p');
        judgeLine.className = 'case-meta';
        judgeLine.textContent = 'Judge decisions (seeded / swapped): ' + item.judges.map((judge) =>
          judge.model + ' ' + ((judge.order_checks || []).map((order) => order.passed ? 'pass' : 'fail').join(' / '))
        ).join(' · ') + ' · both must pass';
        card.append(judgeLine);
      }
      if (item.first_pass_summary) {
        const firstPass = document.createElement('p');
        firstPass.className = 'case-meta';
        firstPass.textContent = 'First-pass checks: active ' + item.first_pass_summary.active.passed_runs + '/'
          + item.first_pass_summary.active.total_runs + ', candidate ' + item.first_pass_summary.candidate.passed_runs + '/'
          + item.first_pass_summary.candidate.total_runs + ' · repairs: active '
          + item.first_pass_summary.active.repair_attempts + ', candidate '
          + item.first_pass_summary.candidate.repair_attempts;
        card.append(firstPass);
      }

      const issues = [
        ...(item.baseline_check && item.baseline_check.issues || []).map((message) => 'Active: ' + message),
        ...(item.candidate_check && item.candidate_check.issues || []).map((message) => 'Candidate: ' + message),
        ...(item.judge_issues || []),
      ];
      if (issues.length) {
        const issue = document.createElement('p');
        issue.className = 'case-issues';
        issue.textContent = [...new Set(issues)].join(' | ');
        card.append(issue);
      } else if (item.judge_summary) {
        const note = document.createElement('p');
        note.className = 'case-meta';
        note.textContent = item.judge_summary;
        card.append(note);
      }

      if (item.candidate_answer || item.baseline_answer) {
        const details = document.createElement('details');
        details.className = 'case-answer';
        const summaryLine = document.createElement('summary');
        summaryLine.textContent = 'View all paired answers (' + Math.max((item.baseline_answers || []).length, (item.candidate_answers || []).length, 1) + ' runs)';
        details.append(summaryLine);
        const baselineRuns = item.baseline_runs || [];
        const candidateRuns = item.candidate_runs || [];
        const baselineAnswers = item.baseline_answers || [item.baseline_answer || ''];
        const candidateAnswers = item.candidate_answers || [item.candidate_answer || ''];
        const runCount = Math.max(baselineRuns.length, candidateRuns.length, baselineAnswers.length, candidateAnswers.length);
        for (let index = 0; index < runCount; index += 1) {
          const runHeading = document.createElement('strong');
          runHeading.textContent = 'Paired run ' + (index + 1);
          details.append(runHeading);
          [
            ['Active', baselineRuns[index], baselineAnswers[index]],
            ['Candidate', candidateRuns[index], candidateAnswers[index]],
          ].forEach(([side, run, finalAnswer]) => {
            const heading = document.createElement('strong');
            const firstCheck = run && run.first_pass_check;
            heading.textContent = side + (firstCheck ? ' · first pass ' + (firstCheck.passed ? 'passed' : 'failed')
              + ' · ' + (run.repair_attempts || 0) + ' repairs' : ' answer');
            details.append(heading);
            const first = document.createElement('pre');
            first.textContent = run ? run.first_answer || '' : finalAnswer || '';
            details.append(first);
            if (run && run.first_issues && run.first_issues.length) {
              const firstIssues = document.createElement('small');
              firstIssues.className = 'case-issues';
              firstIssues.textContent = 'First-draft rules: ' + run.first_issues.join(' · ');
              details.append(firstIssues);
            }
            (run && run.repair_history || []).forEach((repair) => {
              const repairHeading = document.createElement('strong');
              repairHeading.textContent = 'Repair ' + repair.attempt + (repair.passed ? ' · passed' : ' · still failed');
              const repairAnswer = document.createElement('pre');
              repairAnswer.textContent = repair.answer || '';
              details.append(repairHeading, repairAnswer);
            });
            const finalHeading = document.createElement('strong');
            finalHeading.textContent = side + ' final answer';
            const content = document.createElement('pre');
            content.textContent = run ? run.final_answer || '' : finalAnswer || '';
            details.append(finalHeading, content);
          });
        }
        card.append(details);
      }
      caseList.append(card);
    });
  }

  async function loadApplications() {
    const result = await api('/api/control/applications/summary');
    state.applications = result.applications || [];
    state.judgeModels = result.judge_models || [];
    state.judgePanelConfigured = Boolean(result.judge_panel_configured);
    state.caseCount = Number(result.case_count ?? 0);
    state.coreCaseCount = Number(result.core_case_count ?? state.caseCount);
    state.holdoutCaseCount = Number(result.holdout_case_count ?? 0);
    state.repetitions = Number(result.repetitions ?? 3);
    $('#regressionCaseCount').textContent = String(state.caseCount);
    $('#regressionSummary').textContent = state.coreCaseCount + ' core + ' + state.holdoutCaseCount
      + ' reserved cases · ' + state.repetitions + ' runs/model · '
      + (state.judgeModels.length ? state.judgeModels.length + (state.judgeModels.length === 1 ? ' judge configured' : ' judges configured') : 'judge required');
    $('#evaluateButton').textContent = evaluateButtonLabel();
    $('#finalEvaluateButton').textContent = finalEvaluationButtonLabel();
    const select = $('#compareApplication');
    const selected = select.value;
    select.replaceChildren();
    if (!state.applications.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No tutor applications are configured';
      select.append(option);
      select.disabled = true;
    } else {
      state.applications.forEach((application) => {
        const option = document.createElement('option');
        option.value = application.application_id;
        option.textContent = application.display_name;
        option.dataset.personaId = application.persona_id || application.application_id;
        select.append(option);
      });
      if (state.applications.some((application) => application.application_id === selected)) select.value = selected;
      select.disabled = false;
    }
    updateButtons();
  }

  async function loadHealth() {
    try {
      const health = await api('/api/health', {}, false);
      $('#serviceStatus').textContent = health.ok ? 'Parent online - Azure ready' : 'Azure configuration required';
      $('#serviceStatus').className = 'service-pill' + (health.ok ? ' online' : ' offline');
    } catch (error) {
      $('#serviceStatus').textContent = error.status === 503 ? 'Parent online - Azure not configured' : 'Parent status unavailable';
      $('#serviceStatus').className = 'service-pill offline';
    }
  }

  async function loadState() {
    try {
      const data = await api('/api/control/state');
      render(data);
      await loadApplications();
      return true;
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        sessionStorage.removeItem(tokenKey);
        $('#serviceStatus').textContent = 'Admin login required';
        $('#activeModel').textContent = 'Connect to view release';
        $('#activeRelease').textContent = '';
        $('#candidateOverview').textContent = 'Locked';
        $('#candidateStatus').textContent = 'Admin access required';
        $('#candidateModel').disabled = true;
        $('#evaluateButton').disabled = true;
        $('#finalEvaluateButton').disabled = true;
        $('#compareApplication').disabled = true;
        $('#compareButton').disabled = true;
        $('#promoteButton').disabled = true;
        if (!authDialog.open) authDialog.showModal();
        showNotice('Enter the parent admin token to load release controls.', 'info');
      } else {
        showNotice(error.message, 'error');
      }
      return false;
    }
  }

  async function stageCandidateIfNeeded() {
    const model = $('#candidateModel').value.trim();
    if (!model) throw new Error('Enter the candidate Azure deployment name first.');
    if (!state.data || model !== (state.data.candidate && state.data.candidate.model)) {
      const result = await api('/api/control/candidate/model', {
        method: 'PUT',
        body: JSON.stringify({ model }),
      });
      state.data.candidate = result.candidate;
      state.data.last_evaluation = null;
      render(state.data);
    }
    return model;
  }

  function renderComparison(result) {
    resultPanel.replaceChildren();
    const heading = document.createElement('strong');
    heading.textContent = result.active_model + ' compared with ' + result.candidate_model;
    resultPanel.append(heading);
    const context = document.createElement('small');
    context.textContent = 'Same context sent to both models: ' + (result.context_source || 'student records');
    resultPanel.append(context);
    const guidance = document.createElement('p');
    guidance.className = 'help-text comparison-guidance';
    guidance.textContent = 'The final answer is shown first. Open each draft history to inspect the unedited first response and any repairs. A live tutor request withholds a final answer that still fails its rules. This comparison does not promote the candidate.';
    resultPanel.append(guidance);

    const columns = document.createElement('div');
    columns.className = 'comparison-columns';
    [
      ['Active answer', result.baseline_answer, result.baseline_check, result.baseline_repair_attempts,
        result.baseline_first_answer, result.baseline_first_pass_check, result.baseline_repair_history],
      ['Candidate answer', result.candidate_answer, result.candidate_check, result.candidate_repair_attempts,
        result.candidate_first_answer, result.candidate_first_pass_check, result.candidate_repair_history],
    ].forEach(([titleText, answer, check, repairs, firstAnswer, firstCheck, repairHistory]) => {
      const article = document.createElement('article');
      article.className = 'comparison-answer';
      const title = document.createElement('h3');
      title.textContent = titleText + ' (' + (repairs || 0) + ' repairs)';
      const text = document.createElement('pre');
      text.textContent = answer || '';
      const status = document.createElement('strong');
      status.className = check && check.passed ? 'check-pass' : 'check-fail';
      status.textContent = check && check.passed ? 'Record and response checks passed' : 'Review required';
      article.append(title, text, status);
      if (check && check.issues && check.issues.length) {
        const issues = document.createElement('p');
        issues.className = 'case-issues';
        issues.textContent = check.issues.join(' · ');
        article.append(issues);
      }
      const draftHistory = document.createElement('details');
      draftHistory.className = 'draft-history';
      const draftSummary = document.createElement('summary');
      draftSummary.textContent = 'First draft: ' + (firstCheck && firstCheck.passed ? 'passed' : 'failed')
        + ' · ' + (repairs || 0) + ' repairs';
      draftHistory.append(draftSummary);
      const firstHeading = document.createElement('strong');
      firstHeading.textContent = 'Unedited first draft';
      const firstText = document.createElement('pre');
      firstText.textContent = firstAnswer || '';
      draftHistory.append(firstHeading, firstText);
      if (firstCheck && firstCheck.issues && firstCheck.issues.length) {
        const firstIssues = document.createElement('p');
        firstIssues.className = 'case-issues';
        firstIssues.textContent = firstCheck.issues.join(' · ');
        draftHistory.append(firstIssues);
      }
      (repairHistory || []).forEach((repair) => {
        const repairHeading = document.createElement('strong');
        repairHeading.textContent = 'Repair ' + repair.attempt + (repair.passed ? ' · passed' : ' · still failed');
        const repairText = document.createElement('pre');
        repairText.textContent = repair.answer || '';
        draftHistory.append(repairHeading, repairText);
      });
      article.append(draftHistory);
      columns.append(article);
    });
    resultPanel.append(columns);
    if (result.behavior_review) {
      const review = document.createElement('section');
      review.className = 'behavior-review';
      const reviewTitle = document.createElement('strong');
      reviewTitle.textContent = result.behavior_review.status === 'possible_drift'
        ? 'Possible behavior drift detected'
        : result.behavior_review.status === 'unavailable'
          ? 'Behavior review unavailable'
          : result.behavior_review.status === 'human_review_only'
            ? 'Human review required'
            : 'No material drift detected in this sample';
      const reviewSummary = document.createElement('p');
      reviewSummary.textContent = result.behavior_review.summary || '';
      review.append(reviewTitle, reviewSummary);
      const reviewIssues = [
        ...(result.behavior_review.issues || []),
        ...Object.entries(result.behavior_review.score_drops || {}).map(([name, scores]) =>
          name.replaceAll('_', ' ') + ' score: active ' + scores.active + '/5, candidate ' + scores.candidate + '/5'),
      ];
      if (reviewIssues.length) {
        const issue = document.createElement('p');
        issue.className = 'case-issues';
        issue.textContent = reviewIssues.join(' | ');
        review.append(issue);
      }
      if (result.behavior_review.judges && result.behavior_review.judges.length) {
        const judgeLine = document.createElement('p');
        judgeLine.className = 'case-meta';
        judgeLine.textContent = 'Judge decisions: ' + result.behavior_review.judges.map((judge) =>
          judge.model + ' ' + (judge.passed ? 'passed' : 'failed')).join(' · ')
          + ' · every judge must pass both blind answer orders';
        review.append(judgeLine);
      }
      const caveat = document.createElement('small');
      caveat.textContent = result.behavior_review.status === 'human_review_only'
        ? 'This comparison is not a promotion check. The automated judge uses saved synthetic cases only.'
        : 'This panel reviews one sample only; repeated saved cases determine promotion eligibility.';
      review.append(caveat);
      resultPanel.append(review);
    }
    resultPanel.hidden = false;
  }

  $('#authOpen').addEventListener('click', () => authDialog.showModal());
  $('#authForm').addEventListener('submit', async (event) => {
    if (event.submitter && event.submitter.value === 'cancel') return;
    event.preventDefault();
    const token = $('#adminToken').value.trim();
    if (!token) return;
    sessionStorage.setItem(tokenKey, token);
    $('#adminToken').value = '';
    try {
      await api('/api/control/state');
      authDialog.close();
      showNotice('Connected to the parent control plane.', 'success');
      await loadState();
    } catch (error) {
      sessionStorage.removeItem(tokenKey);
      showNotice(error.message, 'error');
    }
  });

  $('#refreshButton').addEventListener('click', async () => {
    await loadHealth();
    await loadState();
  });

  $('#candidateModel').addEventListener('input', () => {
    const model = $('#candidateModel').value.trim();
    $('#candidateOverview').textContent = model || 'Not staged';
    const changed = model && state.data && model !== state.data.candidate.model;
    $('#candidateStatus').textContent = changed
      ? 'Will stage when you compare or run checks'
      : state.data && state.data.last_evaluation ? 'Latest check: ' + state.data.last_evaluation.status : 'Enter a deployment below';
    if (changed) {
      $('#gatePill').className = 'gate-pill';
      $('#gatePill').querySelector('span').textContent = 'Checks required for this deployment';
    } else if (state.data) {
      renderEvaluation(state.data.last_evaluation);
    }
    renderCompatibilityGuidance();
    updateButtons();
  });
  $('#compareApplication').addEventListener('change', () => {
    renderCompatibilityGuidance();
    updateButtons();
  });
  $('#compareQuestion').addEventListener('input', updateButtons);

  $('#saveCompatibilityGuidance').addEventListener('click', async () => {
    const application = selectedApplication();
    if (!application) return;
    setBusy(true, 'save');
    try {
      await stageCandidateIfNeeded();
      const result = await api('/api/control/candidate/applications/' + encodeURIComponent(application.application_id) + '/compatibility', {
        method: 'PUT',
        body: JSON.stringify({ guidance: $('#compatibilityGuidance').value }),
      });
      state.data.candidate = result.candidate;
      state.data.last_evaluation = null;
      render(state.data);
      showNotice('Candidate guidance saved. Run the checks again before promotion.', 'success');
    } catch (error) {
      showNotice(error.message, 'error');
    } finally {
      setBusy(false);
    }
  });

  $('#compareForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = $('#compareQuestion').value.trim();
    if (!question) { showNotice('Enter the tutor question, including the student name or ID.', 'error'); return; }
    setBusy(true, 'compare');
    resultPanel.hidden = false;
    resultPanel.textContent = 'Sending the same question and student context to both deployments...';
    try {
      await stageCandidateIfNeeded();
      const result = await api('/api/control/compare', {
        method: 'POST',
        body: JSON.stringify({
          application_id: $('#compareApplication').value,
          question,
        }),
      });
      renderComparison(result);
    } catch (error) {
      resultPanel.textContent = error.message;
      showNotice(error.message, 'error');
    } finally {
      setBusy(false);
    }
  });

  async function runEvaluation(suiteStage) {
    const isFinal = suiteStage === 'final';
    setBusy(true, isFinal ? 'evaluate-final' : 'evaluate-core');
    try {
      await stageCandidateIfNeeded();
      $('#evaluationSummary').classList.add('running');
      $('#evaluationSummary').replaceChildren();
      const title = document.createElement('strong');
      title.textContent = isFinal ? 'Running final validation' : 'Running core review';
      const description = document.createElement('p');
      description.textContent = isFinal
        ? 'The parent is comparing active and candidate answers across core and reserved cases.'
        : 'The parent is comparing active and candidate answers across core cases. This review cannot promote the candidate.';
      $('#evaluationSummary').append(title, description);
      const result = await api('/api/control/evaluate', {
        method: 'POST',
        body: JSON.stringify({ suite_stage: suiteStage }),
      });
      await loadState();
      let latest = result;
      while (latest && latest.status === 'running') {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        if (!await loadState()) throw new Error('Could not refresh the evaluation status. Check the parent server and refresh the dashboard.');
        latest = state.data && state.data.last_evaluation;
      }
      showNotice(
        (latest && latest.summary) || 'Behavior checks finished.',
        latest && latest.promotion_allowed ? 'success' : latest && latest.status === 'passed' ? 'info' : 'error'
      );
    } catch (error) {
      showNotice(error.message, 'error');
    } finally {
      $('#evaluationSummary').classList.remove('running');
      setBusy(false);
    }
  }

  $('#evaluateButton').addEventListener('click', () => runEvaluation('core'));
  $('#finalEvaluateButton').addEventListener('click', () => runEvaluation('final'));

  $('#promoteButton').addEventListener('click', async () => {
    if (!state.data || !state.data.last_evaluation || !state.data.last_evaluation.promotion_allowed) return;
    const model = state.data.candidate.model;
    if (!window.confirm('Promote ' + model + '? This changes the model serving tutor requests.')) return;
    try {
      const result = await api('/api/control/promote', { method: 'POST' });
      render(result.state);
      showNotice(result.release.release_id + ' is now active on ' + result.release.model + '.', 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });

  $('#rollbackButton').addEventListener('click', async () => {
    if (!window.confirm('Roll tutor requests back to the previous release?')) return;
    try {
      const result = await api('/api/control/rollback', { method: 'POST' });
      render(result.state);
      showNotice('Rolled back to ' + result.release.model + '.', 'success');
    } catch (error) { showNotice(error.message, 'error'); }
  });

  window.setInterval(() => {
    if (adminToken() && state.data && state.data.last_evaluation && state.data.last_evaluation.status === 'running') loadState();
  }, 3000);

  loadHealth();
  loadState();
})();
