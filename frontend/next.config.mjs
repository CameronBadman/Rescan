/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export: the app is entirely client-side and talks to the Rescan
  // API over CORS, so it is hosted as files behind CloudFront.
  output: 'export',
  trailingSlash: true,
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
}

export default nextConfig
