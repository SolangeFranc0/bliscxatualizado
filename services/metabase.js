/**
 * MetabaseService — autenticação, cache e fetch de cards via API Metabase.
 * Token armazenado apenas em memória. Renovação automática em 401.
 * Cache de 5 minutos por card ID.
 */
class MetabaseService {
  constructor() {
    const cfg = window.METABASE_CONFIG || {};
    this.baseUrl  = (cfg.url || '').replace(/\/$/, '');
    this.email    = cfg.email    || '';
    this.password = cfg.password || '';
    this._token   = null;
    this._tokenExpiry = 0;
    this._authPending = null;
    this._cache   = new Map();
    this.CACHE_TTL = 5 * 60 * 1000; // 5 min
  }

  get isConfigured() {
    return !!(this.baseUrl && this.email && this.password);
  }

  // ── Autenticação ─────────────────────────────────────────────────────────
  async _doAuth() {
    const res = await fetch(`${this.baseUrl}/api/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: this.email, password: this.password }),
    });
    if (!res.ok) throw new Error(`Autenticação Metabase falhou (HTTP ${res.status})`);
    const { id } = await res.json();
    this._token = id;
    this._tokenExpiry = Date.now() + 12 * 3600 * 1000; // renova após 12h
  }

  async _ensureToken() {
    if (this._token && Date.now() < this._tokenExpiry) return;
    if (!this._authPending) {
      this._authPending = this._doAuth().finally(() => { this._authPending = null; });
    }
    await this._authPending;
  }

  // ── Cache ─────────────────────────────────────────────────────────────────
  _cacheGet(k) {
    const e = this._cache.get(k);
    if (!e || Date.now() > e.exp) { this._cache.delete(k); return null; }
    return e.v;
  }
  _cacheSet(k, v) {
    this._cache.set(k, { v, exp: Date.now() + this.CACHE_TTL });
  }

  // ── Fetch com retry em 401 ────────────────────────────────────────────────
  async _fetch(url, opts = {}, retried = false) {
    await this._ensureToken();
    const res = await fetch(url, {
      ...opts,
      headers: {
        'Content-Type': 'application/json',
        'X-Metabase-Session': this._token,
        ...(opts.headers || {}),
      },
    });
    if (res.status === 401 && !retried) {
      this._token = null;
      return this._fetch(url, opts, true);
    }
    if (!res.ok) {
      const msg = await res.text().catch(() => '');
      throw new Error(`Metabase API ${res.status}: ${msg.slice(0, 120)}`);
    }
    return res.json();
  }

  // ── Executar card existente ────────────────────────────────────────────────
  async runCard(cardId) {
    if (!cardId) return null;
    const cacheKey = `card:${cardId}`;
    const cached = this._cacheGet(cacheKey);
    if (cached) return cached;
    const data = await this._fetch(
      `${this.baseUrl}/api/card/${cardId}/query`,
      { method: 'POST', body: '{}' }
    );
    this._cacheSet(cacheKey, data);
    return data;
  }

  // ── Limpar cache (ex: ao clicar em Atualizar) ─────────────────────────────
  clearCache() { this._cache.clear(); }

  // ── Helpers para extrair dados ────────────────────────────────────────────
  static rows(res)  { return res?.data?.rows || []; }
  static cols(res)  { return (res?.data?.cols || []).map(c => c.name); }
  static toObjects(res) {
    const cols = MetabaseService.cols(res);
    return MetabaseService.rows(res).map(row =>
      Object.fromEntries(cols.map((name, i) => [name, row[i]]))
    );
  }
}

// Instância global — usa window.METABASE_CONFIG definido antes desta tag
window.MB = new MetabaseService();
