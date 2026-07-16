'use client';
import { StytchProvider as ProviderActual } from '@stytch/nextjs';
import { createStytchUIClient } from '@stytch/nextjs/ui';
import { ReactNode } from 'react';

// Initialize the Stytch client using the project's public token
// (Stytch dashboard → Project settings → API keys → public_token).
const stytch = createStytchUIClient(process.env.NEXT_PUBLIC_STYTCH_PUBLIC_TOKEN || '');

const StytchProvider = ({ children }: { children: ReactNode }) => {
  return <ProviderActual stytch={stytch}>{children}</ProviderActual>;
};

export default StytchProvider;
