/* facebook.js — Facebook post browser for Civic Media */

const PAGE_SIZE = 50;

let currentOffset = 0;
let totalCount    = 0;
let selectedPage  = '';   // page_name filter

const searchInput  = document.getElementById('search-input');
const dateFrom     = document.getElementById('date-from');
const dateTo       = document.getElementById('date-to');
const filterBtn    = document.getElementById('filter-btn');
const clearBtn     = document.getElementById('clear-btn');
const listEl       = document.getElementById('posts-list');
const countEl      = document.getElementById('post-count');
const paginationEl = document.getElementById('pagination-bar');
const pageInfoEl   = document.getElementById('page-info');
const prevBtn      = document.getElementById('prev-btn');
const nextBtn      = document.getElementById('next-btn');
const pageListEl   = document.getElementById('page-list');


// ── Utilities ─────────────────────────────────────────────────────────────────

function buildParams(offset = 0) {
  const p = new URLSearchParams();
  const q  = searchInput.value.trim();
  const df = dateFrom.value;
  const dt = dateTo.value;
  if (q)  p.set('q', q);
  if (selectedPage) p.set('page_name', selectedPage);
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
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function initials(name) {
  if (!name) return '?';
  return name.split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

function fmtCount(n) {
  if (n == null) return null;
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}


// ── Page sidebar ──────────────────────────────────────────────────────────────

async function loadPages() {
  try {
    const res = await fetch('/api/facebook/pages');
    if (!res.ok) return;
    const pages = await res.json();
    pages.forEach(p => {
      const item = document.createElement('div');
      item.className = 'fb-page-item';
      item.dataset.page = p.page_name;
      item.innerHTML = `<span>${escHtml(p.page_name)}</span><span class="fb-page-count">${p.post_count}</span>`;
      item.addEventListener('click', () => {
        pageListEl.querySelectorAll('.fb-page-item').forEach(el => el.classList.remove('active'));
        item.classList.add('active');
        selectedPage = p.page_name;
        loadPage(0);
      });
      pageListEl.appendChild(item);
    });
  } catch (e) {
    console.warn('Could not load pages:', e);
  }
}


// ── Count ─────────────────────────────────────────────────────────────────────

async function fetchCount() {
  const p = buildParams(0);
  p.delete('limit');
  p.delete('offset');
  try {
    const res = await fetch(`/api/facebook/posts/count?${p}`);
    if (!res.ok) return 0;
    const { count } = await res.json();
    return count;
  } catch { return 0; }
}


// ── Render ────────────────────────────────────────────────────────────────────

function renderCard(post) {
  const card = document.createElement('div');
  card.className = 'fb-card';
  card.dataset.id = post.post_id;

  const engParts = [];
  if (post.reaction_count != null) engParts.push(`<span>${fmtCount(post.reaction_count)}</span> reactions`);
  if (post.comment_count != null)  engParts.push(`<span>${fmtCount(post.comment_count)}</span> comments`);
  if (post.share_count != null)    engParts.push(`<span>${fmtCount(post.share_count)}</span> shares`);
  if (post.view_count != null)     engParts.push(`<span>${fmtCount(post.view_count)}</span> views`);

  const sharedHtml = post.shared_from_url
    ? `<div class="fb-shared-badge">&#x21AA; Shared post</div>`
    : '';

  card.innerHTML = `
    <div class="fb-card-header">
      <div class="fb-avatar">${initials(post.author_name || post.page_name)}</div>
      <div class="fb-card-body">
        <div class="fb-author-row">
          <span class="fb-author-name">${escHtml(post.author_name || 'Unknown')}</span>
          ${post.page_name ? `<span class="fb-page-badge">${escHtml(post.page_name)}</span>` : ''}
          <span class="fb-timestamp">${escHtml(fmtDate(post.published_at))}</span>
        </div>
        ${post.post_text ? `<div class="fb-post-text">${escHtml(post.post_text)}</div>` : ''}
        ${engParts.length ? `<div class="fb-engagement">${engParts.map(e => `<span class="fb-eng-item">${e}</span>`).join('')}</div>` : ''}
        ${sharedHtml}
      </div>
      <span class="fb-chevron">&#x276F;</span>
    </div>
    <div class="fb-detail">
      <div class="fb-detail-actions">
        <a href="${escHtml(post.post_url)}" target="_blank" rel="noopener">Open on Facebook &#x2197;</a>
        ${post.shared_from_url ? `<a href="${escHtml(post.shared_from_url)}" target="_blank" rel="noopener">Original post &#x2197;</a>` : ''}
      </div>
      ${renderMedia(post.media_urls)}
      ${post.post_text
        ? `<div class="fb-full-text">${escHtml(post.post_text)}</div>`
        : '<p style="color:#666;font-size:.78rem;margin:0 0 10px">No text content.</p>'}
      <div class="fb-comments-section" id="comments-${post.post_id}">
        ${post.db_comment_count > 0
          ? `<div class="fb-comments-title">Comments (${post.db_comment_count})</div><div class="loading-state" style="font-size:.75rem;padding:4px 0">Click to load…</div>`
          : ''}
      </div>
    </div>
  `;

  const header = card.querySelector('.fb-card-header');
  header.addEventListener('click', () => {
    const wasExpanded = card.classList.contains('expanded');
    card.classList.toggle('expanded');
    // Lazy-load comments on first expand
    if (!wasExpanded && post.db_comment_count > 0) {
      const commentsEl = card.querySelector('.fb-comments-section');
      if (!commentsEl.dataset.loaded) {
        loadComments(post.post_id, commentsEl);
        commentsEl.dataset.loaded = '1';
      }
    }
  });

  return card;
}

function renderMedia(mediaUrls) {
  if (!mediaUrls || !mediaUrls.length) return '';
  const imgs = mediaUrls
    .filter(u => u && !u.startsWith('data:'))
    .slice(0, 8)
    .map(u => `<img src="${escHtml(u)}" alt="" loading="lazy"
                    onclick="window.open('${escHtml(u)}','_blank')" />`)
    .join('');
  return imgs ? `<div class="fb-media-strip">${imgs}</div>` : '';
}


// ── Comments ──────────────────────────────────────────────────────────────────

async function loadComments(postId, containerEl) {
  try {
    const res = await fetch(`/api/facebook/posts/${postId}/comments?threaded=true&limit=50`);
    if (!res.ok) {
      containerEl.innerHTML = '<div class="fb-comments-title">Comments</div><p style="color:#666;font-size:.75rem">Failed to load.</p>';
      return;
    }
    const comments = await res.json();
    if (!comments.length) {
      containerEl.innerHTML = '<div class="fb-comments-title">Comments</div><p style="color:#666;font-size:.75rem">No comments stored.</p>';
      return;
    }
    let html = `<div class="fb-comments-title">Comments (${comments.length})</div>`;
    comments.forEach(c => {
      html += `
        <div class="fb-comment">
          <span class="fb-comment-author">${escHtml(c.author_name || 'Unknown')}</span>
          <div class="fb-comment-text">${escHtml(c.comment_text || '')}</div>
          <div class="fb-comment-meta">
            ${c.published_at ? fmtDate(c.published_at) : ''}
            ${c.reaction_count ? ` · ${c.reaction_count} reactions` : ''}
            ${c.reply_count ? ` · ${c.reply_count} replies` : ''}
          </div>
        </div>`;
    });
    containerEl.innerHTML = html;
  } catch (e) {
    containerEl.innerHTML = '<div class="fb-comments-title">Comments</div><p style="color:#666;font-size:.75rem">Error loading comments.</p>';
  }
}


// ── Load page ─────────────────────────────────────────────────────────────────

async function loadPage(offset = 0) {
  currentOffset = offset;
  listEl.innerHTML = '<div class="loading-state">Loading…</div>';

  const [posts, count] = await Promise.all([
    fetch(`/api/facebook/posts?${buildParams(offset)}`).then(r => r.json()).catch(() => []),
    fetchCount(),
  ]);
  totalCount = count;

  countEl.textContent = `${totalCount.toLocaleString()} post${totalCount !== 1 ? 's' : ''}`;

  if (!posts.length) {
    listEl.innerHTML = '<div class="loading-state" style="color:#666">No posts found.</div>';
    paginationEl.style.display = 'none';
    return;
  }

  listEl.innerHTML = '';
  posts.forEach(p => listEl.appendChild(renderCard(p)));

  const totalPages = Math.ceil(totalCount / PAGE_SIZE);
  const curPage    = Math.floor(offset / PAGE_SIZE) + 1;
  pageInfoEl.textContent = `Page ${curPage} of ${totalPages} (${totalCount.toLocaleString()} total)`;
  prevBtn.disabled = offset === 0;
  nextBtn.disabled = offset + PAGE_SIZE >= totalCount;
  paginationEl.style.display = totalCount > PAGE_SIZE ? 'flex' : 'none';
}


// ── Event listeners ───────────────────────────────────────────────────────────

filterBtn.addEventListener('click', () => loadPage(0));

searchInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') loadPage(0);
});

clearBtn.addEventListener('click', () => {
  searchInput.value = '';
  dateFrom.value = '';
  dateTo.value = '';
  selectedPage = '';
  pageListEl.querySelectorAll('.fb-page-item').forEach(el => el.classList.remove('active'));
  pageListEl.querySelector('[data-page=""]').classList.add('active');
  loadPage(0);
});

// "All pages" click handler
pageListEl.querySelector('[data-page=""]').addEventListener('click', () => {
  pageListEl.querySelectorAll('.fb-page-item').forEach(el => el.classList.remove('active'));
  pageListEl.querySelector('[data-page=""]').classList.add('active');
  selectedPage = '';
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
  await loadPages();
  await loadPage(0);
})();
