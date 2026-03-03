/* articles.js — News article browser for Civic Media */

const PAGE_SIZE = 50;

let currentOffset = 0;
let totalCount    = 0;

const searchInput  = document.getElementById('search-input');
const sourceSelect = document.getElementById('source-select');
const dateFrom     = document.getElementById('date-from');
const dateTo       = document.getElementById('date-to');
const filterBtn    = document.getElementById('filter-btn');
const clearBtn     = document.getElementById('clear-btn');
const listEl       = document.getElementById('articles-list');
const countEl      = document.getElementById('article-count');
const paginationEl = document.getElementById('pagination-bar');
const pageInfoEl   = document.getElementById('page-info');
const prevBtn      = document.getElementById('prev-btn');
const nextBtn      = document.getElementById('next-btn');


// ── Utilities ─────────────────────────────────────────────────────────────────

function buildParams(offset = 0) {
  const p = new URLSearchParams();
  const q  = searchInput.value.trim();
  const s  = sourceSelect.value;
  const df = dateFrom.value;
  const dt = dateTo.value;
  if (q)  p.set('q', q);
  if (s)  p.set('source_slug', s);
  if (df) p.set('date_from', df);
  if (dt) p.set('date_to', dt);
  p.set('limit', PAGE_SIZE);
  p.set('offset', offset);
  return p;
}

function fmtDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  } catch { return iso.slice(0, 10); }
}

function escHtml(s) {
  return String(s || '')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function slugLabel(slug) {
  const map = {
    'shasta-scout':           'Shasta Scout',
    'record-searchlight':     'Record Searchlight',
    'redding-record-searchlight': 'Record Searchlight',
  };
  return map[slug] || slug.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}


// ── Source dropdown ───────────────────────────────────────────────────────────

async function loadSources() {
  try {
    const res = await fetch('/api/articles/sources');
    if (!res.ok) return;
    const sources = await res.json();
    sources.forEach(({ source_slug, count }) => {
      const opt = document.createElement('option');
      opt.value = source_slug;
      opt.textContent = `${slugLabel(source_slug)} (${count})`;
      sourceSelect.appendChild(opt);
    });
  } catch (e) {
    console.warn('Could not load sources:', e);
  }
}


// ── Count ─────────────────────────────────────────────────────────────────────

async function fetchCount() {
  const p = buildParams(0);
  // Remove pagination params — count endpoint doesn't use them
  p.delete('limit');
  p.delete('offset');
  try {
    const res = await fetch(`/api/articles/count?${p}`);
    if (!res.ok) return 0;
    const { count } = await res.json();
    return count;
  } catch { return 0; }
}


// ── Render ────────────────────────────────────────────────────────────────────

function renderCard(article) {
  const card = document.createElement('div');
  card.className = 'article-card';
  card.dataset.id = article.article_id;

  const excerpt = article.description || (article.article_text || '').slice(0, 240);

  card.innerHTML = `
    <div class="article-card-header">
      <div class="article-meta">
        <span class="article-source-badge">${escHtml(slugLabel(article.source_slug))}</span>
        <span class="article-date">${escHtml(fmtDate(article.published_at))}</span>
      </div>
      <div class="article-body">
        <div class="article-headline">${escHtml(article.headline || '(no headline)')}</div>
        ${article.author ? `<div class="article-author">${escHtml(article.author)}</div>` : ''}
        ${excerpt ? `<div class="article-excerpt">${escHtml(excerpt)}</div>` : ''}
      </div>
      <span class="article-chevron">&#x276F;</span>
    </div>
    <div class="article-detail">
      <div class="article-detail-actions">
        <a href="${escHtml(article.canonical_url)}" target="_blank" rel="noopener">Open article ↗</a>
      </div>
      ${renderImages(article.image_urls)}
      ${article.article_text
        ? `<div class="article-full-text">${escHtml(article.article_text)}</div>`
        : '<p style="color:#666;font-size:.78rem;margin:0 0 10px">Full text not available.</p>'}
      ${renderLinks(article.embedded_links)}
      ${renderTags(article.source_tags)}
    </div>
  `;

  card.querySelector('.article-card-header').addEventListener('click', () => {
    card.classList.toggle('expanded');
  });

  return card;
}

function renderImages(imageUrls) {
  if (!imageUrls || !imageUrls.length) return '';
  const imgs = imageUrls
    .filter(u => u && !u.startsWith('data:'))
    .slice(0, 8)
    .map(u => `<img src="${escHtml(u)}" alt="" loading="lazy"
                    onclick="window.open('${escHtml(u)}','_blank')" />`)
    .join('');
  return imgs ? `<div class="article-image-strip">${imgs}</div>` : '';
}

function renderLinks(links) {
  if (!links || !links.length) return '';
  const items = links.slice(0, 20).map(l =>
    `<li><a href="${escHtml(l.href)}" target="_blank" rel="noopener">${escHtml(l.text || l.href)}</a></li>`
  ).join('');
  return `
    <div class="article-links-section">
      <div class="article-links-title">Embedded links (${links.length})</div>
      <ul class="article-links-list">${items}</ul>
    </div>`;
}

function renderTags(tags) {
  if (!tags || !tags.length) return '';
  const chips = tags.map(t => `<span class="article-tag">${escHtml(t)}</span>`).join('');
  return `<div class="article-tags-row">${chips}</div>`;
}


// ── Load page ─────────────────────────────────────────────────────────────────

async function loadPage(offset = 0) {
  currentOffset = offset;
  listEl.innerHTML = '<div class="loading-state">Loading…</div>';

  const [articles, count] = await Promise.all([
    fetch(`/api/articles?${buildParams(offset)}`).then(r => r.json()).catch(() => []),
    fetchCount(),
  ]);
  totalCount = count;

  countEl.textContent = `${totalCount.toLocaleString()} article${totalCount !== 1 ? 's' : ''}`;

  if (!articles.length) {
    listEl.innerHTML = '<div class="loading-state" style="color:#666">No articles found.</div>';
    paginationEl.style.display = 'none';
    return;
  }

  listEl.innerHTML = '';
  articles.forEach(a => listEl.appendChild(renderCard(a)));

  // Pagination
  const totalPages = Math.ceil(totalCount / PAGE_SIZE);
  const curPage    = Math.floor(offset / PAGE_SIZE) + 1;
  pageInfoEl.textContent = `Page ${curPage} of ${totalPages} (${totalCount.toLocaleString()} total)`;
  prevBtn.disabled = offset === 0;
  nextBtn.disabled = offset + PAGE_SIZE >= totalCount;
  paginationEl.style.display = totalCount > PAGE_SIZE ? 'flex' : 'none';

  // If URL has an article_id, auto-expand it
  const pathParts = window.location.pathname.split('/');
  const urlId = pathParts[pathParts.length - 1];
  if (urlId && urlId.length === 36) {
    const target = listEl.querySelector(`[data-id="${urlId}"]`);
    if (target) {
      target.classList.add('expanded');
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }
}


// ── Event listeners ───────────────────────────────────────────────────────────

filterBtn.addEventListener('click', () => loadPage(0));

searchInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') loadPage(0);
});

clearBtn.addEventListener('click', () => {
  searchInput.value = '';
  sourceSelect.value = '';
  dateFrom.value = '';
  dateTo.value = '';
  loadPage(0);
});

prevBtn.addEventListener('click', () => {
  if (currentOffset > 0) loadPage(Math.max(0, currentOffset - PAGE_SIZE));
});

nextBtn.addEventListener('click', () => {
  if (currentOffset + PAGE_SIZE < totalCount) loadPage(currentOffset + PAGE_SIZE);
});


// ── Init ──────────────────────────────────────────────────────────────────────

(async function init() {
  await loadSources();
  await loadPage(0);
})();
