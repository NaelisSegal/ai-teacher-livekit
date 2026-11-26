import 'katex/dist/katex.min.css';
import '../styles/globals.css';
import type { Metadata } from 'next';
import { SITE_NAME } from '@/lib/site';

export const metadata: Metadata = {
  title: SITE_NAME,
  description: 'Live classes taught by an AI lecturer, from open materials.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      {/* Browser extensions add their own classes to <body> before the page loads.
          That is harmless, so React is told not to warn about it. */}
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
