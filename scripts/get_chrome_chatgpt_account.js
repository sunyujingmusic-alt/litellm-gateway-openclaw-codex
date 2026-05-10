#!/usr/bin/env node
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

function fail(message, extra = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: message, ...extra }) + '\n');
  process.exit(0);
}

function decryptChromeValue(encryptedHex) {
  const password = execFileSync('security', ['find-generic-password', '-w', '-s', 'Chrome Safe Storage'], { encoding: 'utf8' }).trim();
  const key = crypto.pbkdf2Sync(Buffer.from(password, 'utf8'), Buffer.from('saltysalt', 'utf8'), 1003, 16, 'sha1');
  const iv = Buffer.alloc(16, 0x20);
  let enc = Buffer.from(encryptedHex, 'hex');
  if (enc.subarray(0, 3).toString() === 'v10') enc = enc.subarray(3);
  const decipher = crypto.createDecipheriv('aes-128-cbc', key, iv);
  decipher.setAutoPadding(true);
  return Buffer.concat([decipher.update(enc), decipher.final()]).toString('utf8');
}

function stripGarbage(text) {
  const urlJson = text.indexOf('%7B');
  if (urlJson >= 0) return decodeURIComponent(text.slice(urlJson));
  const json = text.indexOf('{');
  if (json >= 0) return text.slice(json);
  return text.replace(/^[^\x20-\x7E]+/, '');
}

function loadLocalState(baseDir) {
  const p = path.join(baseDir, 'Local State');
  if (!fs.existsSync(p)) return {};
  try {
    return JSON.parse(fs.readFileSync(p, 'utf8'));
  } catch (_) {
    return {};
  }
}

function listProfiles(baseDir, localState) {
  const infoCache = (((localState || {}).profile || {}).info_cache) || {};
  const ids = new Set();
  for (const key of Object.keys(infoCache)) ids.add(key);
  for (const name of fs.readdirSync(baseDir)) {
    const full = path.join(baseDir, name);
    let stat;
    try {
      stat = fs.statSync(full);
    } catch (_) {
      continue;
    }
    if (!stat.isDirectory()) continue;
    if (name === 'Default' || /^Profile \d+$/.test(name)) ids.add(name);
  }
  return Array.from(ids).sort().map((profileId) => {
    const meta = infoCache[profileId] || {};
    return {
      profileId,
      profileName: String(meta.name || profileId),
      googleAccount: String(meta.user_name || ''),
      profileDir: path.join(baseDir, profileId),
    };
  });
}

function getCookieDbPath(profileDir) {
  const direct = path.join(profileDir, 'Cookies');
  if (fs.existsSync(direct)) return direct;
  const network = path.join(profileDir, 'Network', 'Cookies');
  if (fs.existsSync(network)) return network;
  return '';
}

function readAuthCookie(profile) {
  const src = getCookieDbPath(profile.profileDir);
  if (!src) return { ...profile, ok: false, error: 'chrome_cookies_db_missing', cookieDbPath: '' };
  const tmp = path.join(os.tmpdir(), `chrome-cookies-${process.pid}-${profile.profileId.replace(/[^a-zA-Z0-9_-]/g, '_')}.sqlite`);
  fs.copyFileSync(src, tmp);
  try {
    const query = `select host_key,name,hex(encrypted_value) from cookies where (host_key='chatgpt.com' or host_key='.chatgpt.com') and name='oai-client-auth-info' order by host_key desc limit 1;`;
    const out = execFileSync('/usr/bin/sqlite3', ['-separator', '\t', tmp, query], { encoding: 'utf8' }).trim();
    if (!out) return { ...profile, ok: false, error: 'chatgpt_auth_cookie_missing', cookieDbPath: src, cookiesMtimeMs: fs.statSync(src).mtimeMs };
    const [host, name, encryptedHex] = out.split('\t');
    const decrypted = decryptChromeValue(encryptedHex || '');
    const cleaned = stripGarbage(decrypted);
    let parsed;
    try {
      parsed = JSON.parse(cleaned);
    } catch (_) {
      return { ...profile, ok: false, error: 'chatgpt_auth_cookie_parse_failed', host, cookieName: name, cookieDbPath: src, preview: cleaned.slice(0, 400), cookiesMtimeMs: fs.statSync(src).mtimeMs };
    }
    const user = parsed && typeof parsed === 'object' ? parsed.user : null;
    const email = user && typeof user === 'object' ? String(user.email || '') : '';
    const nameValue = user && typeof user === 'object' ? String(user.name || '') : '';
    return {
      ...profile,
      ok: true,
      host,
      cookieName: name,
      email,
      name: nameValue,
      rawUser: user || null,
      cookieDbPath: src,
      cookiesMtimeMs: fs.statSync(src).mtimeMs,
    };
  } finally {
    try { fs.unlinkSync(tmp); } catch (_) {}
  }
}

function chooseActiveProfile(results, localState) {
  const valid = results.filter((r) => r.ok && r.email);
  if (!valid.length) return { selected: null, source: 'no_valid_chatgpt_profile' };

  const lastUsed = String((((localState || {}).profile || {}).last_used) || '').trim();
  if (lastUsed) {
    const hit = valid.find((r) => r.profileId === lastUsed);
    if (hit) return { selected: hit, source: 'last_used' };
  }

  const lastActive = ((((localState || {}).profile || {}).last_active_profiles) || []).map((x) => String(x || '').trim()).filter(Boolean);
  for (let i = lastActive.length - 1; i >= 0; i -= 1) {
    const hit = valid.find((r) => r.profileId === lastActive[i]);
    if (hit) return { selected: hit, source: 'last_active_profiles' };
  }

  if (valid.length === 1) return { selected: valid[0], source: 'single_chatgpt_profile' };

  valid.sort((a, b) => (Number(b.cookiesMtimeMs || 0) - Number(a.cookiesMtimeMs || 0)) || a.profileId.localeCompare(b.profileId));
  return { selected: valid[0], source: 'latest_cookie_mtime' };
}

function main() {
  const baseDir = path.join(os.homedir(), 'Library/Application Support/Google/Chrome');
  if (!fs.existsSync(baseDir)) return fail('chrome_base_dir_missing', { path: baseDir });
  const localState = loadLocalState(baseDir);
  const profiles = listProfiles(baseDir, localState);
  if (!profiles.length) return fail('chrome_profiles_missing', { path: baseDir });

  const results = profiles.map(readAuthCookie);
  const picked = chooseActiveProfile(results, localState);
  if (!picked.selected) {
    return fail('chatgpt_auth_cookie_missing', {
      profiles: results.map((r) => ({
        profileId: r.profileId,
        profileName: r.profileName,
        googleAccount: r.googleAccount,
        ok: !!r.ok,
        error: r.error || '',
      })),
    });
  }

  const selected = picked.selected;
  process.stdout.write(JSON.stringify({
    ok: true,
    selectionSource: picked.source,
    selectedProfileId: selected.profileId,
    selectedProfileName: selected.profileName,
    selectedGoogleAccount: selected.googleAccount,
    host: selected.host,
    cookieName: selected.cookieName,
    email: selected.email,
    name: selected.name,
    rawUser: selected.rawUser || null,
    profiles: results.map((r) => ({
      profileId: r.profileId,
      profileName: r.profileName,
      googleAccount: r.googleAccount,
      ok: !!r.ok,
      email: r.email || '',
      name: r.name || '',
      error: r.error || '',
      cookiesMtimeMs: Number(r.cookiesMtimeMs || 0),
    })),
  }) + '\n');
}

main();
