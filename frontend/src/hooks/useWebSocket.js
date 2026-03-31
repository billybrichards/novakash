import { useEffect, useState, useCallback, useRef } from 'react'
import { useAuth } from '../auth/AuthContext.jsx'

/**
 * useWebSocket — Hook for WebSocket connection with auto-reconnect.
 *
 * Usage:
 *   const { isConnected, data, send } = useWebSocket('/ws/feed')
 *   useEffect(() => {
 *     if (data?.type === 'trade') { ... }
 *   }, [data])
 */
export function useWebSocket(path) {
  const { accessToken } = useAuth()
  const [isConnected, setIsConnected] = useState(false)
  const [data, setData] = useState(null)
  const wsRef = useRef(null)
  const reconnectTimeoutRef = useRef(null)
  const [reconnectAttempts, setReconnectAttempts] = useState(0)

  const connect = useCallback(() => {
    if (!accessToken) return

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const host = window.location.host
    const url = `${protocol}//${host}${path}?token=${accessToken}`

    try {
      wsRef.current = new WebSocket(url)

      wsRef.current.onopen = () => {
        setIsConnected(true)
        setReconnectAttempts(0)
      }

      wsRef.current.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          setData(msg)
        } catch (err) {
          console.error('Failed to parse WebSocket message', err)
        }
      }

      wsRef.current.onerror = (error) => {
        console.error('WebSocket error:', error)
      }

      wsRef.current.onclose = () => {
        setIsConnected(false)
        // Auto-reconnect with exponential backoff
        const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000)
        reconnectTimeoutRef.current = setTimeout(() => {
          setReconnectAttempts(prev => prev + 1)
          connect()
        }, delay)
      }
    } catch (err) {
      console.error('Failed to create WebSocket:', err)
    }
  }, [accessToken, path, reconnectAttempts])

  const send = useCallback((message) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(message))
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      wsRef.current?.close()
      clearTimeout(reconnectTimeoutRef.current)
    }
  }, [connect])

  return { isConnected, data, send }
}
