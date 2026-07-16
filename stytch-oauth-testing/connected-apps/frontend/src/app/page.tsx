'use client';

import { useEffect } from 'react';
import { useStytchUser } from '@stytch/nextjs';
import { Login, Logout } from '@/components/Auth';

export default function Home() {
  const { user, isInitialized } = useStytchUser();

  // After login, bounce back to the OAuth request that sent the user here
  // (stored by withLoginRequired on /oauth/authorize). This is the single,
  // reliable trigger — it fires whenever the session becomes available, no
  // matter how login completed, and survives <Login/> unmounting. Hard
  // navigation so the IdentityProvider re-reads the query params with the new
  // session and can issue the code + redirect back to the MCP client.
  useEffect(() => {
    if (!isInitialized || !user) return;
    const returnTo = localStorage.getItem('returnTo');
    if (returnTo) {
      localStorage.removeItem('returnTo');
      window.location.href = returnTo;
    }
  }, [isInitialized, user]);

  if (!isInitialized) return <p>Loading…</p>;

  if (user) {
    return (
      <div>
        <p>
          Signed in as <strong>{user.emails?.[0]?.email ?? user.user_id}</strong>.
        </p>
        <p>
          If an MCP client sent you here to authorize, you&apos;ll be redirected back
          automatically. Otherwise, start an OAuth flow from your client.
        </p>
        <Logout />
      </div>
    );
  }

  return (
    <div>
      <p>Sign in to authorize MCP clients to access the Couchbase MCP server.</p>
      <Login />
    </div>
  );
}
