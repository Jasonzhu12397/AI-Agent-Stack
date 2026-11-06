export type ConnectorTemplate = {
  id: string;
  label: string;
  summary: string;
  host: string;
  auth: "none" | "credential_ref";
  credentialHint: string;
  payload: Record<string, unknown>;
};

const objectSchema = (
  properties: Record<string, unknown>,
  required: string[] = [],
): Record<string, unknown> => ({
  type: "object",
  properties,
  required,
  additionalProperties: false,
});

const readOnlyTool = (
  name: string,
  description: string,
  inputSchema: Record<string, unknown>,
  outputSchema: Record<string, unknown>,
  permissions: string[],
) => ({
  name,
  description,
  input_schema: inputSchema,
  output_schema: outputSchema,
  required_permissions: permissions,
  risk_level: "low",
  timeout_seconds: 20,
  retry_policy: { max_attempts: 2, backoff_seconds: 2 },
  idempotency_mode: "read_only",
});

export const connectorTemplates: ConnectorTemplate[] = [
  {
    id: "open-meteo",
    label: "Open-Meteo Weather",
    summary: "Current weather through the public Open-Meteo forecast API. No API key is required.",
    host: "api.open-meteo.com",
    auth: "none",
    credentialHint: "No credential required",
    payload: {
      plugin_key: "open-meteo-weather",
      name: "Open-Meteo Weather",
      description: "Read current weather conditions for a latitude and longitude.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Open-Meteo",
      license: "CC BY 4.0",
      source_url: "https://open-meteo.com/",
      manifest: {
        http: {
          base_url: "https://api.open-meteo.com",
          auth_required: false,
          health_check: {
            method: "GET",
            path: "/v1/forecast",
            path_params: {},
            query_params: {
              latitude: "latitude",
              longitude: "longitude",
              current: "current",
            },
            arguments: {
              latitude: 37.7749,
              longitude: -122.4194,
              current: "temperature_2m",
            },
          },
          operations: {
            "read-current-weather": {
              method: "GET",
              path: "/v1/forecast",
              path_params: {},
              query_params: {
                latitude: "latitude",
                longitude: "longitude",
                current: "current",
              },
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "read-current-weather",
          "Read current weather for a geographic coordinate.",
          objectSchema(
            {
              latitude: { type: "number", minimum: -90, maximum: 90 },
              longitude: { type: "number", minimum: -180, maximum: 180 },
              current: {
                type: "string",
                enum: ["temperature_2m", "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"],
              },
            },
            ["latitude", "longitude", "current"],
          ),
          objectSchema({
            latitude: { type: "number" },
            longitude: { type: "number" },
            current: { type: "object", properties: {}, additionalProperties: true },
          }),
          ["weather:current:read"],
        ),
      ],
    },
  },
  {
    id: "amap-places",
    label: "Amap Places",
    summary: "Read-only Amap POI text search. API keys are resolved server-side and injected only into the configured key query parameter.",
    host: "restapi.amap.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a query credential named key.",
    payload: {
      plugin_key: "amap-places",
      name: "Amap Places",
      description: "Search points of interest with the Amap Web Service API.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Amap",
      license: "Amap Web Service Terms",
      source_url: "https://lbs.amap.com/api/webservice/guide/api/search",
      manifest: {
        http: {
          base_url: "https://restapi.amap.com",
          auth_required: true,
          health_check: {
            method: "GET",
            path: "/v3/place/text",
            path_params: {},
            query_params: { keywords: "keywords", city: "city" },
            auth_query_parameter: "key",
            arguments: { keywords: "Beijing", city: "北京" },
          },
          operations: {
            "search-amap-places": {
              method: "GET",
              path: "/v3/place/text",
              path_params: {},
              query_params: {
                keywords: "keywords",
                city: "city",
                citylimit: "city_limit",
                offset: "page_size",
                page: "page",
                extensions: "extensions",
              },
              auth_query_parameter: "key",
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "search-amap-places",
          "Search Amap points of interest by keyword and optional city.",
          objectSchema(
            {
              keywords: { type: "string", minLength: 2, maxLength: 100 },
              city: { type: "string", maxLength: 80 },
              city_limit: { type: "string", enum: ["true", "false"] },
              page_size: { type: "integer", minimum: 1, maximum: 25 },
              page: { type: "integer", minimum: 1, maximum: 100 },
              extensions: { type: "string", enum: ["base", "all"] },
            },
            ["keywords"],
          ),
          objectSchema({
            status: { type: "string" },
            info: { type: "string" },
            count: { type: "string" },
            pois: {
              type: "array",
              items: objectSchema({
                id: { type: "string" },
                name: { type: "string" },
                address: { type: "string" },
                location: { type: "string" },
                type: { type: "string" },
                cityname: { type: "string" },
              }),
            },
          }),
          ["amap:places:read"],
        ),
      ],
    },
  },
  {
    id: "amap-geocoding",
    label: "Amap Geocoding",
    summary: "Read-only address geocoding and reverse geocoding. Amap keys are resolved server-side and injected as the key query parameter.",
    host: "restapi.amap.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a query credential named key.",
    payload: {
      plugin_key: "amap-geocoding",
      name: "Amap Geocoding",
      description: "Convert structured addresses to coordinates and coordinates to structured addresses.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Amap",
      license: "Amap Web Service Terms",
      source_url: "https://lbs.amap.com/api/webservice/guide/api/georegeo",
      manifest: {
        http: {
          base_url: "https://restapi.amap.com",
          auth_required: true,
          health_check: {
            method: "GET",
            path: "/v3/geocode/geo",
            path_params: {},
            query_params: { address: "address", output: "output" },
            auth_query_parameter: "key",
            arguments: { address: "北京市朝阳区阜通东大街6号", output: "JSON" },
          },
          operations: {
            "geocode-address": {
              method: "GET",
              path: "/v3/geocode/geo",
              path_params: {},
              query_params: { address: "address", city: "city", output: "output" },
              auth_query_parameter: "key",
            },
            "reverse-geocode": {
              method: "GET",
              path: "/v3/geocode/regeo",
              path_params: {},
              query_params: {
                location: "location",
                radius: "radius",
                extensions: "extensions",
                poitype: "poi_type",
                roadlevel: "road_level",
                output: "output",
              },
              auth_query_parameter: "key",
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "geocode-address",
          "Convert a structured address into Amap longitude and latitude coordinates.",
          objectSchema({
            address: { type: "string", minLength: 3, maxLength: 200 },
            city: { type: "string", maxLength: 80 },
            output: { type: "string", enum: ["JSON", "XML"] },
          }, ["address"]),
          objectSchema({
            status: { type: "string" },
            info: { type: "string" },
            count: { type: "string" },
            geocodes: { type: "array", items: objectSchema({
              formatted_address: { type: "string" },
              province: { type: "string" },
              city: {},
              district: { type: "string" },
              adcode: { type: "string" },
              location: { type: "string" },
              level: { type: "string" },
            }) },
          }),
          ["amap:geocoding:read"],
        ),
        readOnlyTool(
          "reverse-geocode",
          "Convert longitude and latitude coordinates into a structured address and optional nearby POIs.",
          objectSchema({
            location: { type: "string", pattern: "^-?\\d{1,3}\\.\\d{1,6},-?\\d{1,2}\\.\\d{1,6}$" },
            radius: { type: "integer", minimum: 0, maximum: 3000 },
            extensions: { type: "string", enum: ["base", "all"] },
            poi_type: { type: "string", maxLength: 200 },
            road_level: { type: "string", enum: ["0", "1"] },
            output: { type: "string", enum: ["JSON", "XML"] },
          }, ["location"]),
          objectSchema({
            status: { type: "string" },
            info: { type: "string" },
            regeocode: objectSchema({
              formatted_address: { type: "string" },
              addressComponent: objectSchema({}, []),
              pois: { type: "array", items: objectSchema({}, []) },
            }),
          }),
          ["amap:geocoding:read"],
        ),
      ],
    },
  },
  {
    id: "baidu-map-places",
    label: "Baidu Map Places",
    summary: "Read-only Baidu Place Search. The AK credential is resolved server-side and injected into the configured query parameter.",
    host: "api.map.baidu.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a query credential named ak.",
    payload: {
      plugin_key: "baidu-map-places",
      name: "Baidu Map Places",
      description: "Search points of interest with the Baidu Map Place API.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Baidu",
      license: "Baidu Map Open Platform Terms",
      source_url: "https://lbsyun.baidu.com/index.php?title=webapi/guide/webservice-placeapi",
      manifest: {
        http: {
          base_url: "https://api.map.baidu.com",
          auth_required: true,
          health_check: {
            method: "GET",
            path: "/place/v2/search",
            path_params: {},
            query_params: {
              query: "query",
              region: "region",
              output: "output",
            },
            auth_query_parameter: "ak",
            arguments: {
              query: "coffee",
              region: "Beijing",
              output: "json",
            },
          },
          operations: {
            "search-baidu-places": {
              method: "GET",
              path: "/place/v2/search",
              path_params: {},
              query_params: {
                query: "query",
                region: "region",
                output: "output",
                scope: "scope",
                page_size: "page_size",
                page_num: "page_num",
              },
              auth_query_parameter: "ak",
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "search-baidu-places",
          "Search Baidu Map points of interest by keyword and region.",
          objectSchema(
            {
              query: { type: "string", minLength: 2, maxLength: 100 },
              region: { type: "string", minLength: 1, maxLength: 80 },
              output: { type: "string", enum: ["json", "xml"] },
              scope: { type: "integer", enum: [1, 2] },
              page_size: { type: "integer", minimum: 1, maximum: 20 },
              page_num: { type: "integer", minimum: 0, maximum: 100 },
            },
            ["query", "region", "output"],
          ),
          objectSchema({
            status: { type: "integer" },
            message: { type: "string" },
            total: { type: "integer" },
            results: {
              type: "array",
              items: objectSchema({
                name: { type: "string" },
                address: { type: "string" },
                uid: { type: "string" },
                telephone: { type: "string" },
                detail: { type: "number" },
                location: objectSchema({
                  lat: { type: "number" },
                  lng: { type: "number" },
                }),
              }),
            },
          }),
          ["baidu:places:read"],
        ),
      ],
    },
  },
  {
    id: "google-maps-places",
    label: "Google Maps Places",
    summary: "Read-only Places text search using the Places API (New). Requires a server-side Google API key reference.",
    host: "places.googleapis.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a header credential named X-Goog-Api-Key.",
    payload: {
      plugin_key: "google-maps-places",
      name: "Google Maps Places",
      description: "Search Google Places using read-only Places API text search.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Google",
      license: "Proprietary API",
      source_url: "https://developers.google.com/maps/documentation/places/web-service",
      manifest: {
        http: {
          base_url: "https://places.googleapis.com/v1",
          auth_required: true,
          health_check: {
            method: "POST",
            path: "/places:searchText",
            path_params: {},
            query_params: {},
            body_param: "request",
            read_only: true,
            headers: {
              "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
            },
            arguments: {
              request: { textQuery: "coffee in San Francisco", maxResultCount: 1 },
            },
          },
          operations: {
            "search-places": {
              method: "POST",
              path: "/places:searchText",
              path_params: {},
              query_params: {},
              body_param: "request",
              read_only: true,
              headers: {
                "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
              },
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "search-places",
          "Search Google Maps for places matching a text query.",
          objectSchema(
            {
              request: objectSchema(
                {
                  textQuery: { type: "string", minLength: 2, maxLength: 300 },
                  maxResultCount: { type: "integer", minimum: 1, maximum: 20 },
                },
                ["textQuery"],
              ),
            },
            ["request"],
          ),
          objectSchema({
            places: {
              type: "array",
              items: objectSchema({
                id: { type: "string" },
                displayName: objectSchema({ text: { type: "string" } }),
                formattedAddress: { type: "string" },
              }),
            },
          }),
          ["maps:places:read"],
        ),
      ],
    },
  },
  {
    id: "github-readonly",
    label: "GitHub Read-only",
    summary: "Search repositories through the GitHub REST API. Uses a server-side fine-grained token reference.",
    host: "api.github.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a bearer token with repository read access.",
    payload: {
      plugin_key: "github-readonly",
      name: "GitHub Read-only",
      description: "Search GitHub repositories visible to the configured token.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "GitHub",
      license: "GitHub API Terms",
      source_url: "https://docs.github.com/en/rest",
      manifest: {
        http: {
          base_url: "https://api.github.com",
          auth_required: true,
          health_check: {
            method: "GET",
            path: "/rate_limit",
            path_params: {},
            query_params: {},
          },
          operations: {
            "search-repositories": {
              method: "GET",
              path: "/search/repositories",
              path_params: {},
              query_params: { q: "query", per_page: "per_page" },
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "search-repositories",
          "Search repositories by GitHub search query.",
          objectSchema(
            {
              query: { type: "string", minLength: 2, maxLength: 256 },
              per_page: { type: "integer", minimum: 1, maximum: 100 },
            },
            ["query"],
          ),
          objectSchema({
            total_count: { type: "integer" },
            incomplete_results: { type: "boolean" },
            items: { type: "array", items: objectSchema({}) },
          }),
          ["github:repo:read"],
        ),
      ],
    },
  },
  {
    id: "microsoft-graph-readonly",
    label: "Microsoft Graph Read-only",
    summary: "Read the signed-in Microsoft 365 user's profile. Requires a server-side OAuth access-token reference.",
    host: "graph.microsoft.com",
    auth: "credential_ref",
    credentialHint: "Credential resolver must return a bearer token with the User.Read scope.",
    payload: {
      plugin_key: "microsoft-graph-readonly",
      name: "Microsoft Graph Read-only",
      description: "Read the signed-in Microsoft 365 user profile.",
      version: "1.0.0",
      connector_type: "http",
      maintainer: "Microsoft",
      license: "Microsoft API Terms",
      source_url: "https://learn.microsoft.com/graph/",
      manifest: {
        http: {
          base_url: "https://graph.microsoft.com/v1.0",
          auth_required: true,
          health_check: {
            method: "GET",
            path: "/me",
            path_params: {},
            query_params: {},
          },
          operations: {
            "get-my-profile": {
              method: "GET",
              path: "/me",
              path_params: {},
              query_params: {},
            },
          },
        },
      },
      tools: [
        readOnlyTool(
          "get-my-profile",
          "Read the authenticated Microsoft 365 user's basic profile.",
          objectSchema({}),
          objectSchema({
            displayName: { type: "string" },
            userPrincipalName: { type: "string" },
            id: { type: "string" },
          }),
          ["microsoft-graph:user:read"],
        ),
      ],
    },
  },
];

export function buildConnectorTemplatePayload(templateId: string): Record<string, unknown> {
  const template = connectorTemplates.find((item) => item.id === templateId);
  if (!template) {
    throw new Error("Unknown connector template.");
  }
  return JSON.parse(JSON.stringify(template.payload)) as Record<string, unknown>;
}
