// The URL hash holds the whole view state, so reload and back work.
//   #/2020/trip?filter=fav&tag=trip&sort=name&photo=123   browse a folder (photo = loupe); filter and tag are ANDed
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
  // Ticket 200: the rating filter and the tag are independent; an old link filter=tag:X means tag X.
  let filter = q.get('filter') || 'all';
  let tag = q.get('tag') || null;
  const legacy = /^tag:(.+)$/.exec(filter);
  if (legacy) { filter = 'all'; tag = tag || legacy[1]; }
  return {
    page: 'browse', dir,
    filter, tag,
    sort: q.get('sort') || 'date',
    photo: q.get('photo') ? Number(q.get('photo')) : null,
    recursive: q.get('recursive') === '1',
  };
}

export function href({dir = '.', filter = 'all', tag = null, sort = 'date', photo = null, recursive = false}) {
  const path = dir === '.' ? '/' : '/' + dir.split('/').map(encodeURIComponent).join('/');
  const q = new URLSearchParams();
  if (filter !== 'all') q.set('filter', filter);
  if (tag) q.set('tag', tag);
  if (sort !== 'date') q.set('sort', sort);
  if (photo) q.set('photo', photo);
  if (recursive) q.set('recursive', '1');
  const qs = q.toString();
  return '#' + path + (qs ? '?' + qs : '');
}

export function hrefPage(page, dir, recursive = false) {
  return `#!${page}?dir=${encodeURIComponent(dir)}` + (recursive ? '&recursive=1' : '');
}
