import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from './AuthContext.jsx';

/**
 * LoginPage — dark-themed login form.
 *
 * Submits username + password → JWT to localStorage → redirect to dashboard.
 */
export default function LoginPage() {
  const { login, isAuthenticated } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // Already logged in
  if (isAuthenticated) {
    navigate('/dashboard', { replace: true });
    return null;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await login(username, password);
      navigate('/dashboard', { replace: true });
    } catch (err) {
      setError(err?.response?.data?.detail || 'Login failed. Check credentials.');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      style={{ background: 'var(--bg)', minHeight: '100vh' }}
      className="flex items-center justify-center"
    >
      <div className="card fade-in w-full max-w-sm p-8 mx-4">
        {/* Logo / Title */}
        <div className="mb-8 text-center">
          <div style={{ color: 'var(--accent-purple)' }} className="text-3xl font-bold tracking-tight">
            ₿ BTC Trader
          </div>
          <p style={{ color: 'var(--text-secondary)' }} className="mt-1 text-sm">
            Dashboard login
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Username */}
          <div>
            <label
              htmlFor="username"
              style={{ color: 'var(--text-secondary)' }}
              className="block text-xs font-medium mb-1 uppercase tracking-wider"
            >
              Username
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              required
              autoFocus
              className="w-full rounded-lg px-3 py-2.5 text-sm outline-none transition-colors"
              style={{
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid var(--border)',
                color: 'var(--text-primary)',
              }}
            />
          </div>

          {/* Password */}
          <div>
            <label
              htmlFor="password"
              style={{ color: 'var(--text-secondary)' }}
              className="block text-xs font-medium mb-1 uppercase tracking-wider"
            >
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              className="w-full rounded-lg px-3 py-2.5 text-sm outline-none transition-colors"
              style={{
                background: 'rgba(255,255,255,0.05)',
                border: '1px solid var(--border)',
                color: 'var(--text-primary)',
              }}
            />
          </div>

          {/* Error */}
          {error && (
            <p style={{ color: 'var(--loss)' }} className="text-sm text-center">
              {error}
            </p>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={loading}
            className="w-full rounded-lg py-2.5 text-sm font-semibold transition-opacity"
            style={{
              background: 'var(--accent-purple)',
              color: '#fff',
              opacity: loading ? 0.6 : 1,
            }}
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  );
}
