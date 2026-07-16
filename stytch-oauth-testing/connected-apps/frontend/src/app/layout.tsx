import './globals.css';

import { ReactNode } from 'react';
import { Metadata } from 'next';
import StytchProvider from '@/components/StytchProvider';

export const metadata: Metadata = {
  title: 'Couchbase MCP × Stytch OAuth',
  description: 'Login + consent UI for testing Stytch Connected Apps against the Couchbase MCP server',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <StytchProvider>
      <html lang="en">
        <body>
          <main>
            <h1>Couchbase MCP — Stytch OAuth</h1>
            <div className="container">{children}</div>
          </main>
        </body>
      </html>
    </StytchProvider>
  );
}
