import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import './styles.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The data lives in a SQLite file on this machine, so refetching is nearly free and
      // the pipeline changes it in the background. Short staleness keeps the counts and
      // processing states honest without any manual refresh.
      staleTime: 5_000,
      refetchOnWindowFocus: true,
      retry: (failureCount, error) =>
        // Don't retry a 4xx: a bad id or a rejected payload will fail identically every
        // time, and retrying just delays the error the user needs to see.
        failureCount < 2 &&
        !(typeof error === 'object' && error !== null && 'status' in error &&
          typeof (error as { status: number }).status === 'number' &&
          (error as { status: number }).status >= 400 &&
          (error as { status: number }).status < 500),
    },
  },
})

const root = document.getElementById('root')
if (!root) throw new Error('#root missing from index.html')

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
