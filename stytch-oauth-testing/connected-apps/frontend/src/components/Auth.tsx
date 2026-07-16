'use client';

import React, { useEffect, useMemo } from 'react';
import {
  StytchLogin,
  IdentityProvider as BaseIdentityProvider,
  useStytch,
  useStytchUser,
} from '@stytch/nextjs';
import { OTPMethods, Products, StytchLoginConfig } from '@stytch/vanilla-js';
import { useRouter } from 'next/navigation';

/**
 * Redirect unauthenticated users to "/" (the login page), remembering where
 * they were headed so we can bounce them back after login. This is what gates
 * the /oauth/authorize consent screen behind a Stytch session.
 */
export const withLoginRequired = (Component: React.FC) => {
  const WithLoginRequired = () => {
    const router = useRouter();
    const { user, fromCache, isInitialized } = useStytchUser();

    useEffect(() => {
      if (!isInitialized) return;
      if (!user && !fromCache) {
        localStorage.setItem('returnTo', window.location.href);
        router.push('/');
      }
    }, [user, fromCache, isInitialized, router]);

    if (!user) return null;
    return <Component />;
  };
  WithLoginRequired.displayName = `withLoginRequired(${Component.displayName || Component.name})`;
  return WithLoginRequired;
};

// Hard-navigate back to the OAuth request the user was sent here for (stored by
// withLoginRequired). A full reload makes the IdentityProvider re-read the URL
// params with a now-valid session.
const redirectAfterAuth = () => {
  const returnTo = localStorage.getItem('returnTo');
  localStorage.removeItem('returnTo');
  window.location.href = returnTo || '/';
};

/**
 * Login UI. Defaults to email one-time-passcode (no redirect, no extra
 * provider setup). To also offer Google, enable the Google OAuth provider in
 * the Stytch dashboard and uncomment the oauth bits below.
 */
export const Login = () => {
  const loginConfig = useMemo<StytchLoginConfig>(
    () => ({
      products: [Products.otp /*, Products.oauth */],
      otpOptions: {
        expirationMinutes: 10,
        methods: [OTPMethods.Email],
      },
      // oauthOptions: {
      //   providers: [{ type: OAuthProviders.Google }],
      //   loginRedirectURL: window.location.origin + '/authenticate',
      //   signupRedirectURL: window.location.origin + '/authenticate',
      // },
    }),
    [],
  );

  // No redirect callback here. The home page (page.tsx) watches the session and
  // bounces back to the pending OAuth request once login completes. Relying on
  // StytchLogin's onEvent was racy: this component unmounts the instant the user
  // becomes authenticated, so the event could be missed.
  return <StytchLogin config={loginConfig} />;
};

/**
 * OAuth (e.g. Google) redirect callback. Only used if you enable the oauth
 * product above. Harmless to keep otherwise.
 */
export function Authenticate() {
  const client = useStytch();

  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get('token');
    if (!token) return;
    client.oauth.authenticate(token, { session_duration_minutes: 60 }).then(() => redirectAfterAuth());
  }, [client]);

  return <>Loading…</>;
}

/**
 * The OAuth consent screen, gated behind a Stytch session. Mounted at
 * /oauth/authorize — this is the URL you register as the project's
 * "Authorization URL" in the Stytch dashboard.
 */
export const IdentityProvider = withLoginRequired(BaseIdentityProvider);

export const Logout = function Logout() {
  const stytch = useStytch();
  const { user } = useStytchUser();
  if (!user) return null;
  return (
    <button type="button" className="primary" onClick={() => stytch.session.revoke()}>
      Log out
    </button>
  );
};
