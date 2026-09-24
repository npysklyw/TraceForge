import { render, screen } from '@testing-library/react'
import { expect, test } from 'vitest'
import App from './App'

test('shows the foundation and links to the backend through the local proxy', () => {
  render(<App />)
  expect(screen.getByRole('heading', { name: 'TraceForge' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Check backend health' }))
    .toHaveAttribute('href', '/api/health')
})
