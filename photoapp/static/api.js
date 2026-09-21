// Thin fetch wrappers. Errors carry the server's message; server errors (5xx, for example
// 'database busy') are also shown as a toast right here, so they cannot go unnoticed.

import {toast} from './util.js';

async function request(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch (e) { /* not json */ }
    if (response.status >= 500) toast(detail, true);
    throw new Error(detail);
  }
  return response.json();
}

export const get = (path) => request(path);
export const post = (path, body) => request(path, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const imgUrl = (size, fileId) => `/img/${size}/${fileId}`;
