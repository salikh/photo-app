// Small DOM and formatting helpers shared by all views.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === false || value == null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

let toastTimer = null;
export function toast(message, isError = false) {
  const node = document.getElementById('toast');
  node.textContent = message;
  node.className = 'show' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ''; }, isError ? 5000 : 2200);
}

export function fmtBytes(n) {
  if (n == null) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + ' ' + units[i];
}

export function isTyping(target) {
  return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' ||
                    target.tagName === 'SELECT' || target.isContentEditable);
}

// Serializes async work so rapid key presses reach the server in order.
let chain = Promise.resolve();
export function enqueue(fn) {
  const run = chain.then(fn, fn);
  chain = run.catch(() => {});
  return run;
}

// Retry loading an <img> whose source may still be rendering (404 + Retry-After).
export function retryImage(img, attempts = 6) {
  let n = 0;
  const base = img.getAttribute('src');
  img.addEventListener('error', () => {
    if (n >= attempts) return;
    n++;
    setTimeout(() => { img.src = base + (base.includes('?') ? '&' : '?') + 'r=' + n; }, 1500 * n);
  });
}

// replaceChildren that ignores null/false and flattens arrays (the native
// method would render null as the text "null").
export function setChildren(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}
