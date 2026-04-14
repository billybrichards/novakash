import { useCallback, useMemo } from 'react';
import axios from 'axios';
import { useAuth } from '../auth/AuthContext.jsx';

function normalizeApiPath(url) {
  if (!url || typeof url !== 'string') return url;
  if (url.startsWith('/auth/')) return url;
  if (url.startsWith('/api/')) return url;
  if (url.startsWith('/v4/')) return url;
  if (url.startsWith('/')) return `/api${url}`;
  return `/api/${url}`;
}

/**
 * useApi — Returns an axios-like object with auth headers.
 *
 * Supports both calling conventions used across the codebase:
 *   1. api.get('/trades')           — axios instance style
 *   2. api('GET', '/trading-config') — callable style
 *
 * Base URL is the hub root. Most app calls are normalized to `/api/*`,
 * while root-level routes like `/auth/*` and `/v4/*` are preserved.
 */
export function useApi() {
  const { token, logout } = useAuth();

  return useMemo(() => {
    const apiBase = import.meta.env.VITE_API_URL || '';
    
    const instance = axios.create({
      baseURL: apiBase,
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    instance.interceptors.request.use(config => {
      config.url = normalizeApiPath(config.url);
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
