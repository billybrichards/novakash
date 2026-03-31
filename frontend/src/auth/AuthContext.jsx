import React, { createContext, useContext, useState, useCallback } from 'react';
import { api } from '../lib/api.js';

const AuthContext = createContext(null);

/**
 * AuthProvider — wraps the app, manages JWT state in localStorage.
 *
 * Provides: { token, user, login, logout, isAuthenticated }
 */
export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => localStorage.getItem('access_token'));
  const [user, setUser] = useState(() => {
    const stored = localStorage.getItem('user');
    return stored ? JSON.parse(stored) : null;
  });

  const login = useCallback(async (username, password) => {
    const res = await api.post('/auth/login', { username, password });
    const { access_token, refresh_token } = res.data;

    localStorage.setItem('access_token', access_token);
    localStorage.setItem('refresh_token', refresh_token);
    localStorage.setItem('user', JSON.stringify({ username }));

    setToken(access_token);
    setUser({ username });

    return true;
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem('access_token');
    localStorage.removeItem('refresh_token');
    localStorage.removeItem('user');
    setToken(null);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ token, user, login, logout, isAuthenticated: !!token }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
