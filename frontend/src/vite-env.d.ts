/// <reference types="vite/client" />
interface ImportMetaEnv {
  readonly VITE_CLASSIFICATION?: string;
}
declare module "world-atlas/countries-50m.json" {
  const value: unknown;
  export default value;
}
