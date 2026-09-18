import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  BrowserRouter,
  Link,
  NavLink,
  Route,
  Routes,
  useParams,
} from "react-router-dom";
import {
  QueryClient,
  QueryClientProvider,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import "./style.css";

const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: string | { msg?: string }[] };
      detail = typeof parsed.detail === "string"
        ? parsed.detail
        : Array.isArray(parsed.detail)
          ? parsed.detail.map((item) => item.msg ?? "请求参数有误").join("；")
          : body;
    } catch {
      // The server can also return plain text or an empty response.
    }
    throw new Error(res.status === 404 ? "内容不存在或已删除" : detail || `请求失败（${res.status}）`);
  }
  return res.json() as Promise<T>;
}
function query<T>(key: string, path: string) {
  return useQuery<T>({ queryKey: [key], queryFn: () => api<T>(path) });
}
function QueryStatus({ pending, error, retry }: {
  pending: boolean;
  error: Error | null;
  retry: () => void;
}) {
  if (error) return <div className="notice error" role="alert">加载失败：{error.message} <button className="secondary" onClick={retry}>重试</button></div>;
  if (pending) return <p className="muted" role="status">加载中…</p>;
  return null;
}
const statusLabel: Record<string, string> = {
  pending: "待处理", ready: "已处理", metadata_only: "仅元数据",
  failed: "失败", removed: "已移出收藏", queued: "排队中",
  running: "处理中", done: "完成", cancelled: "已取消",
  succeeded: "成功", process: "处理", always_process: "总是处理",
};
const personalStateLabel: Record<string, string> = {
  want_to_go: "想去", want_to_try: "想试", want_to_learn: "想学",
  visited: "去过", using: "使用中", completed: "已完成",
};
const entityKindLabel: Record<string, string> = {
  restaurant: "餐厅", concept: "概念", place: "地点", product: "产品",
  entity: "知识对象",
};
const searchMethodLabel: Record<string, string> = {
  structured: "结构化", fts: "全文", semantic: "语义",
};
const ruleDimensionLabel: Record<string, string> = {
  source: "单个视频", creator: "作者 ID", collection: "收藏夹",
  semantic_type: "内容类型", keyword: "关键词",
};
const syncKindLabel: Record<string, string> = {
  fixture: "演示数据", file: "文件导入", sidecar: "抖音同步",
  api: "JSON 导入", manual: "手动导入",
};
type Source = {
  id: string;
  title: string;
  creator: string;
  collection: string;
  status: string;
  policy_action: string;
  policy_reason: string;
  url: string;
};
type Claim = {
  claim_id: string;
  text: string;
  evidence_id: string;
  evidence_text: string;
  evidence_kind: string;
  source_id: string;
  source_url: string;
  source_title: string;
};
type Result = {
  source_id: string;
  title: string;
  url: string;
  summary: string;
  methods: string[];
  claims: Claim[];
};
type Answer = {
  scope: string;
  answer: string;
  citations: Claim[];
  results: Result[];
};
function App() {
  return (
    <div className="shell">
      <aside>
        <h1>
          Douyin
          <br />
          Knowledge
        </h1>
        <nav>
          <NavLink to="/">提问</NavLink>
          <NavLink to="/library">收藏</NavLink>
          <NavLink to="/search">搜索</NavLink>
          <NavLink to="/jobs">处理状态</NavLink>
          <NavLink to="/entities">知识对象</NavLink>
          <NavLink to="/resurface">想做的事</NavLink>
          <NavLink to="/wiki">Wiki</NavLink>
          <NavLink to="/settings">设置</NavLink>
        </nav>
        <small>本地优先 · 来源可追溯</small>
      </aside>
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/library" element={<Library />} />
          <Route path="/search" element={<Search />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/sources/:id" element={<SourceDetail />} />
          <Route path="/entities" element={<Entities />} />
          <Route path="/resurface" element={<Resurface />} />
          <Route path="/entities/:id" element={<EntityDetail />} />
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/wiki/:id" element={<WikiDetail />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
function Home() {
  const [question, setQuestion] = useState("");
  const ask = useMutation({
    mutationFn: (q: string) =>
      api<Answer>("/ask", {
        method: "POST",
        body: JSON.stringify({ question: q }),
      }),
  });
  const stats = query<{
    sources: number;
    ready: number;
    metadata_only: number;
    queued: number;
    failed: number;
  }>("dashboard", "/dashboard");
  return (
    <>
      <p className="eyebrow">ASK MY SAVES</p>
      <h2>你以前收藏过什么有用的内容？</h2>
      <p className="muted">提到“我收藏的”时，回答依据已处理的收藏并展示原始证据。通用问题需要配置 AI 服务。</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (question.trim()) ask.mutate(question);
        }}
        className="ask"
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="例如：我收藏的旺角日料人均多少？"
        />
        <button disabled={ask.isPending || !question.trim()}>{ask.isPending ? "回答中…" : "提问"}</button>
      </form>
      <QueryStatus pending={stats.isPending} error={stats.error} retry={() => { void stats.refetch(); }} />
      {stats.data && (
        <div className="stats">
          <span>收藏 {stats.data.sources}</span>
          <span>已处理 {stats.data.ready}</span>
          <span>仅元数据 {stats.data.metadata_only}</span>
          <span>待处理 {stats.data.queued}</span>
          <span>失败 {stats.data.failed}</span>
        </div>
      )}
      {ask.isPending && <p>检索中…</p>}
      {ask.error && <p className="notice error" role="alert">提问失败：{ask.error.message}</p>}
      {ask.data && (
        <section className="card">
          <span className="badge">{ask.data.scope === "personal" ? "我的收藏" : "通用回答"}</span>
          <p className="answer">{ask.data.answer}</p>
          <h3>来源与证据</h3>
          {ask.data.citations.length === 0 ? (
            <p className="muted">没有可引用的收藏证据</p>
          ) : (
            ask.data.citations.map((c) => (
              <div className="citation" key={c.claim_id}>
                <Link to={"/sources/" + c.source_id}>{c.source_title}</Link>
                <p>{c.text}</p>
                <small>
                  {c.evidence_kind} · {c.evidence_text}
                </small>
              </div>
            ))
          )}
        </section>
      )}
    </>
  );
}
function Library() {
  const data = query<Source[]>("sources", "/sources");
  const [filter, setFilter] = useState("");
  const visible = data.data?.filter((s) =>
    (s.title + s.creator + s.collection).toLocaleLowerCase().includes(filter.toLocaleLowerCase()),
  );
  return (
    <>
      <p className="eyebrow">LIBRARY</p>
      <h2>收藏</h2>
      <input
        className="search"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="按标题、作者或收藏夹筛选"
      />
      <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />
      {visible?.length === 0 && <p className="muted">{filter ? "没有匹配的收藏。" : "还没有收藏。可在设置中同步或导入 JSON。"}</p>}
      {visible?.map((s) => (
          <Link className="row" to={"/sources/" + s.id} key={s.id}>
            <strong>{s.title || s.id}</strong>
            <span>
              {s.creator} · {s.collection}
            </span>
            <span className="badge">{statusLabel[s.status] ?? s.status}</span>
          </Link>
        ))}
    </>
  );
}
function Search() {
  const [input, setInput] = useState("");
  const [term, setTerm] = useState("");
  const results = useQuery<Result[]>({
    queryKey: ["search", term],
    queryFn: () => api<Result[]>("/search?q=" + encodeURIComponent(term)),
    enabled: Boolean(term),
  });
  return (
    <>
      <p className="eyebrow">SEARCH</p>
      <h2>搜索已处理的收藏</h2>
      <form className="ask" onSubmit={(event) => { event.preventDefault(); setTerm(input.trim()); }}>
        <input value={input} onChange={(event) => setInput(event.target.value)} placeholder="输入关键词、地点或主题" />
        <button disabled={!input.trim() || results.isFetching}>搜索</button>
      </form>
      {!term && <p className="muted">输入内容后搜索已处理的收藏。</p>}
      {term && <QueryStatus pending={results.isPending} error={results.error} retry={() => { void results.refetch(); }} />}
      {results.data?.length === 0 && <p>没有找到匹配的已处理收藏。</p>}
      {results.data?.map((result) => (
        <section className="card" key={result.source_id}>
          <h3><Link to={"/sources/" + result.source_id}>{result.title}</Link></h3>
          <p>{result.summary}</p>
          <small>匹配方式：{result.methods.map((method) => searchMethodLabel[method] ?? method).join("、")}</small>
          {result.claims.slice(0, 3).map((claim) => <p key={claim.claim_id}>{claim.text}</p>)}
        </section>
      ))}
    </>
  );
}
function Jobs() {
  const jobs = useQuery<{ id: string; source_id: string; source_title: string; status: string; attempts: number; error: string }[]>({
    queryKey: ["jobs"], queryFn: () => api("/jobs"), refetchInterval: 5000,
  });
  return (
    <>
      <p className="eyebrow">PROCESSING</p>
      <h2>处理状态</h2>
      <p className="muted">显示最近的任务，每 5 秒更新一次。</p>
      <QueryStatus pending={jobs.isPending} error={jobs.error} retry={() => { void jobs.refetch(); }} />
      {jobs.data?.length === 0 && <p>暂无处理任务。</p>}
      {jobs.data?.map((job) => (
        <div className="row" key={job.id}>
          <Link to={"/sources/" + job.source_id}>{job.source_title || "查看来源"}</Link>
          <span>{statusLabel[job.status] ?? job.status} · 尝试 {job.attempts} 次</span>
          {job.error && <span className="error">{job.error}</span>}
        </div>
      ))}
    </>
  );
}
function SourceDetail() {
  const { id } = useParams();
  const data = query<{
    source: {
      id: string;
      title: string;
      caption: string;
      url: string;
      status: string;
      policy_action: string;
      policy_reason: string;
      collections: string[];
    };
    evidence: { id: string; kind: string; text: string }[];
    claims: {
      id: string;
      text: string;
      evidence_id: string;
      entity_id: string | null;
    }[];
    runs: {
      id: string;
      status: string;
      provider: string;
      model_name: string;
      level: number;
      error: string;
      result_summary: { evidence_count?: number; claim_count?: number; entity_count?: number };
    }[];
    snapshots: { id: string; checksum: string; observed_at: string }[];
    policy_decisions: {
      id: string;
      action: string;
      reason: string;
      evaluated_at: string;
    }[];
  }>("source-" + id, "/sources/" + id);
  const qc = useQueryClient();
  const process = useMutation({
    mutationFn: () => api("/sources/" + id + "/process", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["source-" + id] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  if (!data.data) return <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />;
  const d = data.data;
  return (
    <>
      <Link to="/library">← 收藏</Link>
      <h2>{d.source.title}</h2>
      <p>{d.source.caption}</p>
      <p className="muted">收藏夹：{d.source.collections.join("、") || "未分组"}</p>
      <p>
        <span className="badge">{statusLabel[d.source.status] ?? d.source.status}</span>{" "}
        {d.source.policy_reason === "default" ? "默认处理" : d.source.policy_reason.startsWith("rule:") ? "匹配处理规则" : d.source.policy_reason}
      </p>
      {d.source.status !== "ready" && d.source.status !== "removed" && <p className="muted">当前来源尚未完成知识处理；历史观点不会参与问答。</p>}
      {d.source.status === "removed" && <p className="muted">该视频已不在最近一次 sidecar 收藏列表中；若重新收藏并同步，历史内容可恢复。</p>}
      {d.source.url && <><a href={d.source.url} target="_blank" rel="noreferrer">打开原视频 ↗</a>{" "}</>}
      <button className="secondary" disabled={d.source.status === "removed" || process.isPending} onClick={() => process.mutate()}>
        重新处理
      </button>
      {process.error && <p className="error">{String(process.error)}</p>}
      {process.isSuccess && <p className="notice success" role="status">已加入处理队列。可在处理状态页查看进度。</p>}
      <section className="card">
        <h3>来源观点</h3>
        {d.claims.length === 0 && <p className="muted">当前没有可用的来源观点。</p>}
        {d.claims.map((c) => (
          <div className="citation" key={c.id}>
            {c.text}
            {c.entity_id && (
              <p>
                <Link to={"/entities/" + c.entity_id}>查看知识对象</Link>
              </p>
            )}
            <small>证据 {c.evidence_id}</small>
          </div>
        ))}
      </section>
      <section className="card">
        <h3>保留的证据（含历史版本）</h3>
        {d.evidence.length === 0 && <p className="muted">尚无文字证据。</p>}
        {d.evidence.map((e) => (
          <p key={e.id}>
            <span className="badge">{e.kind}</span> {e.text}
          </p>
        ))}
      </section>
      <section className="card">
        <h3>处理记录</h3>
        {d.runs.map((r) => (
          <p key={r.id}>
            {statusLabel[r.status] ?? r.status} · {r.provider} / {r.model_name} · Level {r.level} · 证据 {r.result_summary.evidence_count ?? 0} · 观点 {r.result_summary.claim_count ?? 0} {r.error}
          </p>
        ))}
      </section>
      <section className="card">
        <h3>来源版本与策略记录</h3>
        <p>已保存 {d.snapshots.length} 个来源快照</p>
        {d.policy_decisions.map((decision) => (
          <p key={decision.id}>
            {statusLabel[decision.action] ?? decision.action} · {decision.reason === "default" ? "默认策略" : "自定义规则"} · {new Date(decision.evaluated_at).toLocaleString()}
          </p>
        ))}
      </section>
    </>
  );
}
function Entities() {
  const data = query<{ id: string; name: string; kind: string }[]>(
    "entities",
    "/entities",
  );
  return (
    <>
      <p className="eyebrow">ENTITIES</p>
      <h2>知识对象</h2>
      <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />
      {data.data?.length === 0 && <p className="muted">还没有识别出知识对象。处理收藏后再来看。</p>}
      {data.data?.map((e) => (
        <Link className="row" key={e.id} to={"/entities/" + e.id}>
          <strong>{e.name}</strong>
          <span>{entityKindLabel[e.kind] ?? e.kind}</span>
        </Link>
      ))}
    </>
  );
}
function Resurface() {
  const data = query<{
    entity_id: string;
    entity_name: string;
    state: string;
    note: string;
    sources: { source_id: string; title: string; claim_id: string; claim: string }[];
  }[]>("resurface", "/resurface");
  return (
    <>
      <p className="eyebrow">RESURFACE</p>
      <h2>之前想做的事</h2>
      <p className="muted">来自你标记为想去、想试或想学的知识对象，并附上当前可用的收藏来源。</p>
      <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />
      {data.data?.length === 0 && <p>暂无待回看的事项。可在知识对象中标记“想去”。</p>}
      {data.data?.map((card) => (
        <section className="card" key={card.entity_id}>
          <h3><Link to={"/entities/" + card.entity_id}>{card.entity_name}</Link></h3>
          <p>{personalStateLabel[card.state] ?? card.state} {card.note}</p>
          {card.sources.map((source) => (
            <p key={source.claim_id}>{source.claim} · <Link to={"/sources/" + source.source_id}>{source.title}</Link></p>
          ))}
        </section>
      ))}
    </>
  );
}
function EntityDetail() {
  const { id } = useParams();
  const data = query<{
    entity: { name: string; kind: string };
    claims: { id: string; text: string; source_id: string }[];
    user_state: { state: string; rating: number | null; note: string } | null;
  }>("entity-" + id, "/entities/" + id);
  const qc = useQueryClient();
  const [state, setState] = useState("");
  const [note, setNote] = useState("");
  const [rating, setRating] = useState("");
  useEffect(() => {
    setState(data.data?.user_state?.state ?? "");
    setNote(data.data?.user_state?.note ?? "");
    setRating(data.data?.user_state?.rating?.toString() ?? "");
  }, [data.data]);
  const save = useMutation({
    mutationFn: () =>
      api("/entities/" + id + "/state", {
        method: "PUT",
        body: JSON.stringify({ state, note, rating: rating ? Number(rating) : null }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["entity-" + id] });
      qc.invalidateQueries({ queryKey: ["resurface"] });
    },
  });
  if (!data.data) return <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />;
  return (
    <>
      <Link to="/entities">← 知识对象</Link>
      <h2>{data.data.entity.name}</h2>
      <p className="muted">{entityKindLabel[data.data.entity.kind] ?? data.data.entity.kind}</p>
      <section className="card">
        <h3>来源观点</h3>
        {data.data.claims.length === 0 && <p className="muted">当前没有可用的来源观点。</p>}
        {data.data.claims.map((c) => (
          <p key={c.id}>
            {c.text} <Link to={"/sources/" + c.source_id}>查看来源</Link>
          </p>
        ))}
      </section>
      <section className="card">
        <h3>我的体验</h3>
        <p>
          {data.data.user_state?.state ? personalStateLabel[data.data.user_state.state] ?? data.data.user_state.state : "尚未记录"} {data.data.user_state?.note}
        </p>
        <select value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">选择状态</option>
          <option value="want_to_go">想去</option>
          <option value="want_to_try">想试</option>
          <option value="want_to_learn">想学</option>
          <option value="visited">去过</option>
          <option value="using">使用中</option>
          <option value="completed">已完成</option>
        </select>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="个人笔记"
        />
        <select aria-label="个人评分" value={rating} onChange={(e) => setRating(e.target.value)}>
          <option value="">不评分</option>
          {[1, 2, 3, 4, 5].map((value) => <option key={value} value={value}>{value} 分</option>)}
        </select>
        <button disabled={!state || save.isPending} onClick={() => save.mutate()}>
          {save.isPending ? "保存中…" : "保存"}
        </button>
        {save.error && <p className="notice error" role="alert">保存失败：{save.error.message}</p>}
        {save.isSuccess && <p className="notice success" role="status">个人状态已保存。</p>}
      </section>
    </>
  );
}
function Wiki() {
  const data = query<
    { id: string; title: string; kind: string; revision: number }[]
  >("wiki", "/wiki");
  return (
    <>
      <p className="eyebrow">COMPOUNDING WIKI</p>
      <h2>持续积累的知识</h2>
      <p className="muted">
        Wiki 是可重建的整理视图；原始来源与观点保留在数据库中。
      </p>
      <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />
      {data.data?.length === 0 && <p className="muted">还没有 Wiki 页面。</p>}
      {data.data?.map((p) => (
        <Link className="row" key={p.id} to={"/wiki/" + p.id}>
          <strong>{p.title}</strong>
          <span>
            {entityKindLabel[p.kind] ?? p.kind} · 修订 {p.revision}
          </span>
        </Link>
      ))}
    </>
  );
}
function WikiDetail() {
  const { id } = useParams();
  const data = query<{
    title: string;
    body: string;
    revision: number;
    claim_ids: string[];
    supports: { claim_id: string; claim: string; source_id: string; source_title: string }[];
  }>("wiki-" + id, "/wiki/" + id);
  if (!data.data) return <QueryStatus pending={data.isPending} error={data.error} retry={() => { void data.refetch(); }} />;
  return (
    <>
      <Link to="/wiki">← Wiki</Link>
      <h2>{data.data.title}</h2>
      <p className="muted">
        修订 {data.data.revision} · {data.data.claim_ids.length} 条来源支持
      </p>
      <pre className="wiki-body">{data.data.body}</pre>
      <section className="card">
        <h3>来源支持</h3>
        {data.data.supports.length === 0 && <p className="muted">当前修订没有可用的来源观点。</p>}
        {data.data.supports.map((support) => (
          <div className="citation" key={support.claim_id}>
            <p>{support.claim}</p>
            <Link to={"/sources/" + support.source_id}>{support.source_title || "查看来源"}</Link>
          </div>
        ))}
      </section>
    </>
  );
}
function Settings() {
  const qc = useQueryClient();
  const syncHistory = query<{
    id: string;
    kind: string;
    status: string;
    total: number;
    created: number;
    error: string;
    occurred_at: string;
  }[]>("sync-status", "/sync/status");
  const rules = query<
    { id: string; dimension: string; value: string; action: string }[]
  >("rules", "/rules");
  const [dimension, setDimension] = useState("collection");
  const [value, setValue] = useState("");
  const [action, setAction] = useState("metadata_only");
  const [raw, setRaw] = useState("");
  const [notice, setNotice] = useState("");
  const [syncing, setSyncing] = useState(false);
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["rules"] });
    qc.invalidateQueries({ queryKey: ["dashboard"] });
    qc.invalidateQueries({ queryKey: ["sync-status"] });
  };
  async function add() {
    try {
      await api("/rules", {
        method: "POST",
        body: JSON.stringify({ dimension, value, action }),
      });
      setValue("");
      setNotice("规则已添加");
      refresh();
    } catch (e) {
      setNotice(String(e));
    }
  }
  async function upload() {
    try {
      const parsed = JSON.parse(raw);
      const result = await api<{ created: number; total: number }>("/sync", {
        method: "POST",
        body: JSON.stringify(parsed),
      });
      setNotice(`导入 ${result.total} 条，新增 ${result.created} 条`);
      qc.invalidateQueries();
    } catch (e) {
      setNotice(String(e));
    }
  }
  async function syncSidecar() {
    setSyncing(true);
    try {
      const result = await api<{ created: number; total: number }>(
        "/sync/sidecar",
        { method: "POST" },
      );
      setNotice(`同步 ${result.total} 条，新增 ${result.created} 条`);
      qc.invalidateQueries();
    } catch (e) {
      setNotice(String(e));
    } finally {
      setSyncing(false);
    }
  }
  return (
    <>
      <p className="eyebrow">SETTINGS</p>
      <h2>处理策略与导入</h2>
      <section className="card">
        <h3>同步抖音收藏夹</h3>
        <p className="muted">
          需要本机已配置 5.1+ sidecar、API Key 与导入的抖音身份。同步后由后台
          worker 逐步处理。
        </p>
        <button disabled={syncing} onClick={syncSidecar}>
          {syncing ? "同步中…" : "现在同步"}
        </button>
        <p><Link to="/jobs">查看处理状态与失败原因</Link></p>
        <h3>最近同步</h3>
        <QueryStatus pending={syncHistory.isPending} error={syncHistory.error} retry={() => { void syncHistory.refetch(); }} />
        {syncHistory.data?.length === 0 && <p className="muted">暂无同步记录</p>}
        {syncHistory.data?.slice(0, 5).map((event) => (
          <p key={event.id}>
            {new Date(event.occurred_at).toLocaleString()} · {syncKindLabel[event.kind] ?? event.kind} · {statusLabel[event.status] ?? event.status} · {event.total} 条（新增 {event.created}）
            {event.error && <span className="error"> {event.error}</span>}
          </p>
        ))}
      </section>
      <section className="card">
        <h3>导入 JSON 收藏</h3>
        <p className="muted">
          粘贴符合 fixtures/saves.json 格式的数组。敏感 cookie 不进入本应用。
        </p>
        <textarea
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          rows={5}
          placeholder='[{"external_id":"...","title":"...","caption":"..."}]'
        />
        <button onClick={upload}>导入</button>
      </section>
      <section className="card">
        <h3>处理规则</h3>
        <QueryStatus pending={rules.isPending} error={rules.error} retry={() => { void rules.refetch(); }} />
        <div className="formrow">
          <select
            value={dimension}
            onChange={(e) => setDimension(e.target.value)}
          >
            <option value="source">单个视频</option>
            <option value="creator">作者 ID</option>
            <option value="collection">收藏夹</option>
            <option value="semantic_type">内容类型</option>
            <option value="keyword">关键词</option>
          </select>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="匹配值"
          />
          <select value={action} onChange={(e) => setAction(e.target.value)}>
            <option value="metadata_only">仅元数据</option>
            <option value="always_process">总是处理</option>
          </select>
          <button disabled={!value} onClick={add}>
            添加
          </button>
        </div>
        {rules.data?.map((r) => (
          <div className="row" key={r.id}>
            <span>
              {ruleDimensionLabel[r.dimension] ?? r.dimension}: {r.value} → {statusLabel[r.action] ?? r.action}
            </span>
            <button
              className="secondary"
              onClick={async () => {
                try {
                  await api("/rules/" + r.id, { method: "DELETE" });
                  setNotice("规则已删除");
                  refresh();
                } catch (error) {
                  setNotice(`删除失败：${String(error)}`);
                }
              }}
            >
              删除
            </button>
          </div>
        ))}
      </section>
      {notice && <p>{notice}</p>}
    </>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
