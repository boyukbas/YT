// Saves projects (the video plus all edits) in the browser's own database, so a reload
// or coming back later doesn't lose work. Nothing leaves the device.
const DB_NAME = 'shorts-editor';
let dbPromise = null;

function db() {
  dbPromise ??= new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('projects', { keyPath: 'id' });
      req.result.createObjectStore('files');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbPromise;
}
async function tx(stores, mode, fn) {
  const d = await db();
  return new Promise((resolve, reject) => {
    const t = d.transaction(stores, mode);
    let result;
    Promise.resolve(fn(...[].concat(stores).map(s => t.objectStore(s)))).then(r => { result = r; });
    t.oncomplete = () => resolve(result);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  });
}
const req = r => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });

export async function saveFile(id, blob) {
  await tx('files', 'readwrite', s => s.put(blob, id));
  try { await navigator.storage?.persist?.(); } catch {}
}
export const saveProject = (p) => tx('projects', 'readwrite', s => s.put(p));
export const listProjects = async () =>
  (await tx('projects', 'readonly', s => req(s.getAll()))).sort((a, b) => b.updatedAt - a.updatedAt);
export async function loadProject(id) {
  return tx(['projects', 'files'], 'readonly', async (p, f) => {
    const [project, file] = await Promise.all([req(p.get(id)), req(f.get(id))]);
    return project && file ? { ...project, file } : null;
  });
}
export const deleteProject = (id) => tx(['projects', 'files'], 'readwrite', (p, f) => { p.delete(id); f.delete(id); });
