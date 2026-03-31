import { useCallback } from 'react';
import axios from 'axios';
import { useAuth } from '../auth/AuthContext.jsx';

/**
 * useApi — Axios wrapper with JWT auth header.
 *
 * Usage:
 *   const api = useApi();
 *   const res = await api.get('/api/dashboard');
 */
export function useApi() {
  const { token, logout } = useAuth();

  return useCallback(
    (method, url, config = {}) => {
      const instance = axios.create({
        baseURL: '/api',
        headers: {
          Authorization: `Bearer ${token}`,
          ...config.headers,
        },
      });

      instance.interceptors.response.use(
        res => res,
        err => {
          // Logout on 401
          if (err.response?.status === 401) {
            logout();
          }
          return Promise.reject(err);
        }
      );

      return instance({ method, url, ...config });
    },
    [token, logout]
  );
}

// Also export a module-level instance for non-hook contexts
export const api = {
  get: (url, config) => axios.get(url, config),
  post: (url, data, config) => axios.post(url, data, config),
  put: (url, data, config) => axios.put(url, data, config),
  delete: (url, config) => axios.delete(url, config),
};
