const messagesEl = document.querySelector('#chat-messages');
const questionInput = document.querySelector('#question-input');
const sendButton = document.querySelector('#send-button');
const statusEl = document.querySelector('#connection-status');
const tutorSymbol = document.querySelector('.brand-mark')?.textContent?.trim() || 'T';

async function getJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function addMessage(kind, text) {
  const row = document.createElement('div');
  row.className = `message ${kind === 'user' ? 'user-message' : 'assistant-message'}`;
  if (kind !== 'user') {
    const avatar = document.createElement('span');
    avatar.className = 'message-avatar';
    avatar.textContent = tutorSymbol;
    row.append(avatar);
  }
  const bubble = document.createElement('p');
  bubble.textContent = text;
  row.append(bubble);
  messagesEl.append(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function checkDatabase() {
  try {
    const health = await getJson('/api/health');
    statusEl.innerHTML = `<i></i> MongoDB connected (${Number(health.student_count)} students)`;
    statusEl.classList.add('connected');
  } catch {
    statusEl.innerHTML = '<i></i> MongoDB unavailable';
    statusEl.classList.add('disconnected');
    addMessage('assistant', 'I cannot reach MongoDB right now. Start the MongoDB service and refresh this page.');
  }
}

async function askQuestion(question) {
  const text = question.trim();
  if (!text) return;
  addMessage('user', text);
  questionInput.value = '';
  sendButton.disabled = true;
  sendButton.querySelector('span').textContent = 'Thinking...';
  try {
    const result = await getJson('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: text }),
    });
    addMessage('assistant', result.answer);
  } catch (error) {
    addMessage('assistant', error.message);
  } finally {
    sendButton.disabled = false;
    sendButton.querySelector('span').textContent = 'Ask tutor';
    questionInput.focus();
  }
}

document.querySelector('#question-form').addEventListener('submit', event => {
  event.preventDefault();
  askQuestion(questionInput.value);
});

questionInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    askQuestion(questionInput.value);
  }
});

checkDatabase();
