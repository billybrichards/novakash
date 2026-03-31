import React from 'react';
import { Navigate } from 'react-router-dom';
import { useAuth } from './AuthContext.jsx';

/**
 * ProtectedRoute — redirects to /login if no valid token is present.
 *
 * Usage:
 *   <Route element={<ProtectedRoute><Layout /></ProtectedRoute>}>
 *     ...
 *   </Route>
 */
export default function ProtectedRoute({ children }) {
  const { isAuthenticated } = useAuth();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return children;
}
