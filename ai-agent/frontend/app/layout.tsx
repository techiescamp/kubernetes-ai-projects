import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "KubeCheck - Kubernetes triage",
  description: "Diagnoses Kubernetes problems, proposes a fix, and applies it once you approve.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // data-theme is set before paint by the script below, then kept in sync by the app, so the
    // page never flashes the wrong theme on load.
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('kubecheck-theme');if(t!=='light'&&t!=='dark'){t=window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
