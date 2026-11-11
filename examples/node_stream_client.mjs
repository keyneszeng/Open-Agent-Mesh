// Private Mesh SSE consumer (Node.js 18+; no npm dependencies).
// MESH_URL=http://127.0.0.1:8765 MESH_TOKEN=... node examples/node_stream_client.mjs CONVERSATION_ID [after]
const base = (process.env.MESH_URL || 'http://127.0.0.1:8765').replace(/\/$/, '');
const token = process.env.MESH_TOKEN;
const id = process.argv[2];
const after = Number(process.argv[3] || 0);
if (!token || !id || !Number.isSafeInteger(after) || after < 0) {
  console.error('Usage: MESH_TOKEN=... node examples/node_stream_client.mjs CONVERSATION_ID [after]');
  process.exit(2);
}
const url = `${base}/v1/conversations/${encodeURIComponent(id)}/stream?after=${after}&wait_seconds=0.4`;
const response = await fetch(url, { headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' } });
if (!response.ok || !response.body) {
  console.error(`Stream HTTP ${response.status}`);
  process.exit(1);
}
const decoder = new TextDecoder();
let pending = '';
let count = 0;
for await (const chunk of response.body) {
  pending += decoder.decode(chunk, { stream: true });
  let boundary;
  while ((boundary = pending.indexOf('\n\n')) !== -1) {
    const frame = pending.slice(0, boundary).replace(/\r/g, '');
    pending = pending.slice(boundary + 2);
    const line = frame.split('\n').find(s => s.startsWith('data:'));
    if (!line) continue;
    const message = JSON.parse(line.slice(5).trim());
    console.log(JSON.stringify(message));
    count++;
  }
}
if (!count) throw new Error('No SSE messages received');
