import { Public_Sans } from 'next/font/google';
import localFont from 'next/font/local';
import { ThemeProvider } from '@/components/app/theme-provider';
import { ThemeToggle } from '@/components/app/theme-toggle';
import { cn } from '@/lib/shadcn/utils';
import '@/styles/globals.css';

const publicSans = Public_Sans({
  variable: '--font-public-sans',
  subsets: ['latin'],
});

const commitMono = localFont({
  display: 'swap',
  variable: '--font-commit-mono',
  src: [
    {
      path: '../fonts/CommitMono-400-Regular.otf',
      weight: '400',
      style: 'normal',
    },
    {
      path: '../fonts/CommitMono-700-Regular.otf',
      weight: '700',
      style: 'normal',
    },
    {
      path: '../fonts/CommitMono-400-Italic.otf',
      weight: '400',
      style: 'italic',
    },
    {
      path: '../fonts/CommitMono-700-Italic.otf',
      weight: '700',
      style: 'italic',
    },
  ],
});

interface RootLayoutProps {
  children: React.ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={cn(
        publicSans.variable,
        commitMono.variable,
        'scroll-smooth font-sans antialiased'
      )}
    >
      <head>
        {/* Strip attributes injected into the DOM by browser extensions (Urban VPN,
            SpeakIt!, Bitdefender, Grammarly, LanguageTool, ...) before React hydrates.
            React cannot know about them, so they show up as hydration mismatches. */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function () {
  var ATTRS = [
    'bis_skin_checked', 'bis_label', 'bis_size', 'bis_register',
    'cz-shortcut-listen',
    'data-new-gr-c-s-check-loaded', 'data-gr-ext-installed',
    'data-lt-installed', 'data-lt-tmp-id',
    'data-lastpass-icon-root', 'fdprocessedid'
  ];
  function strip(el) {
    if (!el || !el.removeAttribute) return;
    for (var i = 0; i < ATTRS.length; i++) el.removeAttribute(ATTRS[i]);
  }
  function stripTree(el) {
    if (!el || !el.querySelectorAll) return;
    strip(el);
    var all = el.querySelectorAll('*');
    for (var i = 0; i < all.length; i++) strip(all[i]);
  }
  stripTree(document.documentElement);
  var obs = new MutationObserver(function (records) {
    for (var i = 0; i < records.length; i++) {
      var r = records[i];
      if (r.type === 'attributes') {
        strip(r.target);
      } else {
        for (var j = 0; j < r.addedNodes.length; j++) {
          if (r.addedNodes[j].nodeType === 1) stripTree(r.addedNodes[j]);
        }
      }
    }
  });
  obs.observe(document.documentElement, {
    subtree: true, childList: true, attributes: true, attributeFilter: ATTRS
  });
  // Hydration completes within a couple of seconds; stop watching afterwards.
  setTimeout(function () { obs.disconnect(); }, 10000);
})();`,
          }}
        />
        <title>LiveKit Voice Agent</title>
        <meta name="description" content="A voice agent built with LiveKit" />
      </head>
      <body className="overflow-x-hidden" suppressHydrationWarning>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <header className="fixed top-0 left-0 z-50 hidden w-full flex-row justify-between p-6 md:flex">
            <a
              target="_blank"
              rel="noopener noreferrer"
              href="https://livekit.io"
              className="scale-100 transition-transform duration-300 hover:scale-110"
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/lk-logo.svg" alt="LiveKit Logo" className="block size-6 dark:hidden" />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src="/lk-logo-dark.svg"
                alt="LiveKit Logo"
                className="hidden size-6 dark:block"
              />
            </a>
            <span className="text-foreground font-mono text-xs font-bold tracking-wider uppercase">
              Built with{' '}
              <a
                target="_blank"
                rel="noopener noreferrer"
                href="https://docs.livekit.io/agents"
                className="underline underline-offset-4"
              >
                LiveKit Agents
              </a>
            </span>
          </header>

          {children}
          <div className="group fixed bottom-0 left-1/2 z-50 mb-2 -translate-x-1/2">
            <ThemeToggle className="translate-y-20 transition-transform delay-150 duration-300 group-hover:translate-y-0" />
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
