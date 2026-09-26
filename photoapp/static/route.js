// The URL hash holds the whole view state, so reload and back work.
//   #/2020/trip?filter=fav&sort=name&photo=123     browse a folder (photo = loupe)
//   #!activity                                     other pages
//   #!delete-review?dir=2020/trip&recursive=1   other pages scoped to a folder

export function parse() {
  const raw = location.hash.slice(1);
  if (raw.startsWith('!')) {
    const [page, query = ''] = raw.slice(1).split('?');
    const q = new URLSearchParams(query);
    const dir = q.get('dir');
    return {page, dir: dir ? decodeURIComponent(dir) : undefined,
            recursive: q.get('recursive') === '1'};
  }
  const [pathPart, query = ''] = (raw || '/').split('?');
  const q = new URLSearchParams(query);
  const dir = pathPart.split('/').filter(Boolean).map(decodeURIComponent).join('/') || '.';
  return {
    page: 'browse', dir,
    filter: q.get('filter') || 'all',
    sort: q.get('sort') || 'date',
    photo: q.get('photo') ? Number(q.get('photo')) : null,
    recursive: q.get('recursive') === '1',
  };
}

export function href({dir = '.', filter = 'all', sort = 'date', photo = null, recursive = false}) {
  const path = dir === '.' ? '/' : '/' + dir.split('/').map(encodeURIComponent).join('/');
  const q = new URLSearchParams();
  if (filter !== 'all') q.set('filter', filter);
  if (sort !== 'date') q.set('sort', sort);
  if (photo) q.set('photo', photo);
  if (recursive) q.set('recursive', '1');
  const qs = q.toString();
  return '#' + path + (qs ? '?' + qs : '');
}

export function hrefPage(page, dir, recursive = false) {
  return `#!${page}?dir=${encodeURIComponent(dir)}` + (recursive ? '&recursive=1' : '');
}
