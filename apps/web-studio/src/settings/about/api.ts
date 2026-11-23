import { request } from "../../services/http";
import type { AboutInfo } from "./logic";

export function fetchAbout(): Promise<Partial<AboutInfo>> {
  return request("/api/v1/meta/about/");
}
