// Keep the pinned coordination policy; replace only the broken Node HTTPS path.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const cfg = JSON.parse(readFileSync(resolve(root, 'projects/site-agent.json'), 'utf8'));
const checker = cfg.coordination_checker;
const pin = spawnSync('git', ['rev-parse', 'HEAD'], {cwd: dirname(checker), encoding:'utf8'});
if (pin.status !== 0 || pin.stdout.trim() !== cfg.coordination_checker_sha) throw new Error('Checker pin changed');
const { run } = await import(pathToFileURL(checker).href);
const { GitHub } = await import(pathToFileURL(resolve(dirname(checker), 'github.mjs')).href);
const entries = readFileSync(cfg.token.file, 'utf8').split('\n');
const row = entries.find(line => line.startsWith(cfg.token.key + '='));
const token = row?.slice(cfg.token.key.length + 1).trim().replace(/^"|"$/g, '');
if (!token) throw new Error('Missing configured GitHub token');

class IPv4GitHub extends GitHub {
  async request(path, body, method = body === undefined ? 'GET' : 'POST') {
    if (path.startsWith('/') || path.includes('://')) throw new Error('Expected GitHub API-relative path');
    const args = ['-4', '--compressed', '--silent', '--show-error', '--fail-with-body', '--connect-timeout', '8',
      '--max-time', '45', '--config', '-', '--request', method,
      '--header', 'Accept: application/vnd.github+json', '--header', 'Content-Type: application/json'];
    const safeGraphQL = path === 'graphql' &&
      (/^\s*query\b/.test(body?.query || '') ||
       (/^\s*mutation\b/.test(body?.query || '') && /updateProjectV2ItemFieldValue\(/.test(body.query)));
    // Retrying an exact single-select assignment is idempotent. Never retry
    // issue/comment creation or merge requests after an ambiguous response.
    if (method === 'GET' || safeGraphQL) args.push('--retry', '2', '--retry-all-errors');
    if (body !== undefined) args.push('--data-binary', JSON.stringify(body));
    args.push('https://api.github.com/' + path);
    const result = spawnSync('curl', args, {encoding:'utf8', timeout:150000, maxBuffer:10*1024*1024,
      input:'header = ' + JSON.stringify('Authorization: Bearer ' + token) + '\n'});
    if (result.status !== 0) throw new Error(`GitHub ${method} ${path.split('?')[0]} failed (curl ${result.status}: ${result.stderr.slice(0, 180)}); no state assumed`);
    return result.stdout.trim() ? JSON.parse(result.stdout) : null;
  }
}
try {
  const result = await run(process.argv.slice(2), new IPv4GitHub(token));
  console.log(JSON.stringify(result, null, 2));
  if (result.state && result.state !== 'success') process.exitCode = result.state === 'pending' ? 2 : 1;
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
