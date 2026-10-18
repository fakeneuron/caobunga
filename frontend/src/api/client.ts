import axios from 'axios'

import type { FleetStats, ProjectDetail, ProjectsResponse } from './types'

/** Relative base — Vite dev proxy and same-origin deploy both work. */
export const api = axios.create({
  baseURL: '',
  headers: { Accept: 'application/json' },
})

export async function fetchProjects(): Promise<ProjectsResponse> {
  const { data } = await api.get<ProjectsResponse>('/api/projects')
  return data
}

export async function fetchProject(id: string): Promise<ProjectDetail> {
  const { data } = await api.get<ProjectDetail>(`/api/projects/${encodeURIComponent(id)}`)
  return data
}

export async function fetchStats(): Promise<FleetStats> {
  const { data } = await api.get<FleetStats>('/api/stats')
  return data
}
