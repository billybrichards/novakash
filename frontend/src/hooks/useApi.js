import { useCallback, useMemo } from 'react';
import axios from 'axios';
import { useAuth } from '../auth/AuthContext.jsx';

/**
 * useApi — Returns an axios-like object with auth headers.
 *
 * Supports both calling conventions used across the codebase:
 *   1. api.get('/trades')           — axios instance style
 *   2. api('GET', '/trading-config') — callable style
 *
 * Base URL is '/api' — all paths are relative to it.
 * Paths that already start with '/api/' are normalised to avoid double-prefix.
 */
export function useApi() {
  const { token, logout } = useAuth();

  return useMemo(() => {
    const apiBase = import.meta.env.VITE_API_URL
      ? `${import.meta.env.VITE_API_URL}/api`
      : '/api';
    
    const instance = axios.create({
      baseURL: apiBase,
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    instance.interceptors.request.use(config => {
      // Strip leading /api/ to avoid double-prefix (/api/api/...)
      if (config.url && config.url.startsWith('/api/')) {
        config.url = config.url.slice(4); // '/api/foo' → '/foo'
      }
      return config;
    });

    instance.interceptors.response.use(
      res => res,
      err => {
        if (err.response?.status === 401) {
          logout();
        }
        return Promise.reject(err);
      }
    );

    // Make the instance callable: api('GET', '/url', config)
    const callable = (method, url, config = {}) => {
      return instance({ method, url, ...config });
    };

    // Attach axios methods so api.get(), api.post() etc. work too
    callable.get = instance.get.bind(instance);
    callable.post = instance.post.bind(instance);
    callable.put = instance.put.bind(instance);
    callable.delete = instance.delete.bind(instance);
    callable.patch = instance.patch.bind(instance);

    // Expose the raw instance for edge cases
    callable.instance = instance;

    return callable;
  }, [token, logout]);
}

// Module-level instance for non-hook contexts (no auth)
export const api = {
  get: (url, config) => axios.get(url, config),
  post: (url, data, config) => axios.post(url, data, config),
  put: (url, data, config) => axios.put(url, data, config),
  delete: (url, config) => axios.delete(url, config),
};
