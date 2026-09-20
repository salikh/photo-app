// The URL hash holds the whole view state, so reload and back work.
//   #/2020/trip?filter=fav&sort=name&photo=123     browse a folder (photo = loupe)
//   #!activity                                     other pages

export function parse() {
  const raw = location.hash.slice(1);
  if (raw.startsWith('!')) return {page: raw.slice(1).split('?')[0]};
  const [pathPart, query = ''] = (raw || '/').split('?');
  const q = new URLSearchParams(query);
  const dir = pathPart.split('/').filter(Boolean).map(decodeURIComponent).join('/') || '.';
  return {
    page: 'browse', dir,
    filter: q.get('filter') || 'all',
    sort: q.get('sort') || 'date',
    photo: q.get('photo') ? Number(q.get('photo')) : null,
  };
}

export function href({dir = '.', filter = 'all', sort = 'date', photo = null}) {
  const path = dir === '.' ? '/' : '/' + dir.split('/').map(encodeURIComponent).join('/');
  const q = new URLSearchParams();
  if (filter !== 'all') q.set('filter', filter);
  if (sort !== 'date') q.set('sort', sort);
  if (photo) q.set('photo', photo);
  const qs = q.toString();
  return '#' + path + (qs ? '?' + qs : '');
}
