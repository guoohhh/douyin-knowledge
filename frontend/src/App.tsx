import { NavLink, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { api } from './api/client'
import { AskPage } from './pages/AskPage'
import { CollectionsPage } from './pages/CollectionsPage'
import { SourcePage } from './pages/SourcePage'
import { WikiPage } from './pages/WikiPage'
import { WikiDetailPage } from './pages/WikiDetailPage'
import { SettingsPage } from './pages/SettingsPage'
import { ResurfacePage } from './pages/ResurfacePage'
import { EntityPage } from './pages/EntityPage'

function ModeBadge() {
  const { data } = useQuery({ queryKey: ['health'], queryFn: api.health })
  if (!data) return null
  // Worth stating permanently rather than only in settings: in demo mode the answers come
  // from a stand-in model, and someone reading a citation deserves to know that before
  // they trust the sentence above it.
  return <span className="mode">{data.demo_mode ? '演示模式 · 模拟模型' : '已接入真实模型'}</span>
}

export function App() {
  return (
    <div className="shell">
      <header className="masthead">
        <div className="masthead__title">
          收藏知识库
          <small>把刷过就忘的收藏变成能问、能查、能引用的知识</small>
        </div>
        <nav className="nav">
          <NavLink to="/" end>
            提问
          </NavLink>
          <NavLink to="/collections">收藏</NavLink>
          <NavLink to="/wiki">条目</NavLink>
          <NavLink to="/resurface">待办</NavLink>
          <NavLink to="/settings">设置</NavLink>
        </nav>
        <ModeBadge />
      </header>

      <main className="main">
        <Routes>
          <Route path="/" element={<AskPage />} />
          <Route path="/collections" element={<CollectionsPage />} />
          <Route path="/sources/:sourceId" element={<SourcePage />} />
          <Route path="/wiki" element={<WikiPage />} />
          <Route path="/wiki/:pageId" element={<WikiDetailPage />} />
          <Route path="/resurface" element={<ResurfacePage />} />
          <Route path="/entities/:entityId" element={<EntityPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route
            path="*"
            element={
              <div className="empty">
                <h3>没有这个页面</h3>
                <p className="muted">从上面的导航重新开始。</p>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  )
}
