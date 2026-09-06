import { Geist, Geist_Mono } from 'next/font/google'
import type { Metadata, Viewport } from 'next'
import './globals.css'

const geist = Geist({ subsets: ['latin'], variable: '--font-geist' })
const geistMono = Geist_Mono({ subsets: ['latin'], variable: '--font-geist-mono' })

export const metadata: Metadata = {
  title: 'fairmatch. — Fair hiring intelligence',
  description: 'A privacy-first hiring workspace for explainable, fair candidate review.',
}

export const viewport: Viewport = {
  colorScheme: 'light',
  themeColor: '#f7f8f6',
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" className="bg-[#f7f8f6]"><body className={`${geist.variable} ${geistMono.variable} antialiased`}>{children}</body></html>
}
