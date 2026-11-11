// Node.js 18+ with global fetch. MESH_TOKEN=... node examples/node_client.mjs
const url = process.env.MESH_URL || 'http://127.0.0.1:8765';
const token = process.env.MESH_TOKEN;
if (!token) throw Error('Set MESH_TOKEN');
async function api(path, method = 'GET', body) {
  const response = await fetch(url + path, {
    method,
    headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (!response.ok) throw Error(`${response.status}: ${await response.text()}`);
  return response.json();
}
const task = await api('/v1/tasks', 'POST', {to: 'uppercase', input: 'hello from JS'});
let current;
for (let i = 0; i < 100; i++) {
  current = await api(`/v1/tasks/${task.id}`);
  if (['completed', 'failed', 'canceled'].includes(current.state)) break;
  await new Promise(resolve => setTimeout(resolve, 200));
}
console.log(current);
