import { BrowserRouter, Route, Routes } from 'react-router-dom'

import Layout from './components/Layout'
import FleetPage from './pages/FleetPage'
import ProjectDetailPage from './pages/ProjectDetailPage'
import StatsPage from './pages/StatsPage'

export default function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<FleetPage />} />
          <Route path="/projects/:id" element={<ProjectDetailPage />} />
          <Route path="/stats" element={<StatsPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
