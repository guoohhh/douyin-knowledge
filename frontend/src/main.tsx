import React, { useState } from "react";
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

const client = new QueryClient();
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}
function query<T>(key: string, path: string) {
  return useQuery<T>({ queryKey: [key], queryFn: () => api<T>(path) });
}
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
      <p className="muted">提问会优先检索已处理的收藏，并展示原始证据。</p>
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
        <button>提问</button>
      </form>
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
      {ask.error && <p className="error">{String(ask.error)}</p>}
      {ask.data && (
        <section className="card">
          <span className="badge">{ask.data.scope}</span>
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
      {data.data
        ?.filter((s) => (s.title + s.creator + s.collection).includes(filter))
        .map((s) => (
          <Link className="row" to={"/sources/" + s.id} key={s.id}>
            <strong>{s.title || s.id}</strong>
            <span>
              {s.creator} · {s.collection}
            </span>
            <span className="badge">{s.status}</span>
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
        <button>搜索</button>
      </form>
      {results.isPending && term && <p>搜索中…</p>}
      {results.error && <p className="error">{String(results.error)}</p>}
      {results.data?.length === 0 && <p>没有找到匹配的已处理收藏。</p>}
      {results.data?.map((result) => (
        <section className="card" key={result.source_id}>
          <h3><Link to={"/sources/" + result.source_id}>{result.title}</Link></h3>
          <p>{result.summary}</p>
          <small>匹配方式：{result.methods.join("、")}</small>
          {result.claims.slice(0, 3).map((claim) => <p key={claim.claim_id}>{claim.text}</p>)}
        </section>
      ))}
    </>
  );
}
function Jobs() {
  const jobs = query<{ id: string; source_id: string; status: string; attempts: number; error: string }[]>("jobs", "/jobs");
  return (
    <>
      <p className="eyebrow">PROCESSING</p>
      <h2>处理状态</h2>
      <p className="muted">此页面显示最近的任务；刷新页面可查看后台 worker 的最新进度。</p>
      {jobs.data?.length === 0 && <p>暂无处理任务。</p>}
      {jobs.data?.map((job) => (
        <div className="row" key={job.id}>
          <Link to={"/sources/" + job.source_id}>查看来源</Link>
          <span>{job.status} · 尝试 {job.attempts} 次</span>
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
    onSuccess: () => qc.invalidateQueries({ queryKey: ["source-" + id] }),
  });
  if (!data.data) return <p>加载中…</p>;
  const d = data.data;
  return (
    <>
      <Link to="/library">← 收藏</Link>
      <h2>{d.source.title}</h2>
      <p>{d.source.caption}</p>
      <p className="muted">收藏夹：{d.source.collections.join("、") || "未分组"}</p>
      <p>
        <span className="badge">{d.source.status}</span>{" "}
        {d.source.policy_reason}
      </p>
      {d.source.status !== "ready" && <p className="muted">当前来源尚未完成知识处理；历史观点不会参与问答。</p>}
      <a href={d.source.url} target="_blank" rel="noreferrer">
        打开原视频 ↗
      </a>{" "}
      <button className="secondary" onClick={() => process.mutate()}>
        重新处理
      </button>
      <section className="card">
        <h3>来源观点</h3>
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
        <h3>原始证据</h3>
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
            {r.status} · {r.provider} / {r.model_name} · Level {r.level} · 证据 {r.result_summary.evidence_count ?? 0} · 观点 {r.result_summary.claim_count ?? 0} {r.error}
          </p>
        ))}
      </section>
      <section className="card">
        <h3>来源版本与策略记录</h3>
        <p>已保存 {d.snapshots.length} 个来源快照</p>
        {d.policy_decisions.map((decision) => (
          <p key={decision.id}>
            {decision.action} · {decision.reason} · {new Date(decision.evaluated_at).toLocaleString()}
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
      {data.data?.map((e) => (
        <Link className="row" key={e.id} to={"/entities/" + e.id}>
          <strong>{e.name}</strong>
          <span>{e.kind}</span>
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
      {data.data?.length === 0 && <p>暂无待回看的事项。可在知识对象中标记“想去”。</p>}
      {data.data?.map((card) => (
        <section className="card" key={card.entity_id}>
          <h3><Link to={"/entities/" + card.entity_id}>{card.entity_name}</Link></h3>
          <p>{card.state} {card.note}</p>
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
  const save = useMutation({
    mutationFn: () =>
      api("/entities/" + id + "/state", {
        method: "PUT",
        body: JSON.stringify({ state, note }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["entity-" + id] }),
  });
  return (
    <>
      <Link to="/entities">← 知识对象</Link>
      <h2>{data.data?.entity.name}</h2>
      <p className="muted">{data.data?.entity.kind}</p>
      <section className="card">
        <h3>来源观点</h3>
        {data.data?.claims.map((c) => (
          <p key={c.id}>
            {c.text} <Link to={"/sources/" + c.source_id}>查看来源</Link>
          </p>
        ))}
      </section>
      <section className="card">
        <h3>我的体验</h3>
        <p>
          {data.data?.user_state?.state} {data.data?.user_state?.note}
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
        <button disabled={!state} onClick={() => save.mutate()}>
          保存
        </button>
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
      {data.data?.map((p) => (
        <Link className="row" key={p.id} to={"/wiki/" + p.id}>
          <strong>{p.title}</strong>
          <span>
            {p.kind} · 修订 {p.revision}
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
  }>("wiki-" + id, "/wiki/" + id);
  return (
    <>
      <Link to="/wiki">← Wiki</Link>
      <h2>{data.data?.title}</h2>
      <p className="muted">
        修订 {data.data?.revision} · {data.data?.claim_ids.length} 条来源支持
      </p>
      <pre className="wiki-body">{data.data?.body}</pre>
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
        {syncHistory.data?.length === 0 && <p className="muted">暂无同步记录</p>}
        {syncHistory.data?.slice(0, 5).map((event) => (
          <p key={event.id}>
            {new Date(event.occurred_at).toLocaleString()} · {event.kind} · {event.status} · {event.total} 条（新增 {event.created}）
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
              {r.dimension}: {r.value} → {r.action}
            </span>
            <button
              className="secondary"
              onClick={async () => {
                await api("/rules/" + r.id, { method: "DELETE" });
                refresh();
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
