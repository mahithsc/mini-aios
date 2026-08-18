/**
 * Trusted AIOS cloud-control tools for Pi.
 *
 * The Python bridge exposes a finite control-plane protocol. Deployments are
 * manifest-rooted, media reads are confined to the current app workspace, and
 * database access is structured and read-only. Provider credentials and a
 * generic HTTP client are never exposed as tools.
 */

import { createHash } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Type, type Static, type TSchema } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

type DeployPayload = Record<string, unknown> & {
	status: string;
	error?: string;
};

type BridgeToolSpec<TParams extends TSchema> = {
	name: string;
	label: string;
	description: string;
	promptSnippet?: string;
	promptGuidelines?: string[];
	parameters: TParams;
	args: (params: Static<TParams>, toolCallId: string) => string[];
	progress?: (params: Static<TParams>) => string;
};

const BRIDGE_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "../deploy/pi_bridge.py");
const DEPLOY_TIMEOUT_MS = 5 * 60 * 1000;
const OUTPUT_TAIL_LIMIT = 4000;
const FAILURE_STATUSES = new Set(["error", "failed"]);

const APP_ID = Type.String({
	description: "Reserved AIOS cloud app ID",
	pattern: "^app_[A-Za-z0-9]+$",
	maxLength: 128,
});
const RESOURCE_ID = (description: string) =>
	Type.String({
		description,
		minLength: 1,
		maxLength: 128,
		pattern: "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
	});
const DATABASE_NAME = (description: string) =>
	Type.String({
		description,
		minLength: 1,
		maxLength: 128,
		pattern: "^[A-Za-z_][A-Za-z0-9_.-]{0,127}$",
	});

function tail(value: string): string {
	return value.length <= OUTPUT_TAIL_LIMIT ? value : value.slice(-OUTPUT_TAIL_LIMIT);
}

function parsePayload(stdout: string): DeployPayload {
	const value: unknown = JSON.parse(stdout.trim());
	if (typeof value !== "object" || value === null || Array.isArray(value)) {
		throw new Error("bridge response was not a JSON object");
	}
	const payload = value as Record<string, unknown>;
	if (typeof payload.status !== "string") {
		throw new Error("bridge response did not include status");
	}
	return payload as DeployPayload;
}

function stableOperationId(toolCallId: string): string {
	const digest = createHash("sha256")
		.update(`aios-pi-deploy:${toolCallId}`, "utf8")
		.digest("hex");
	return `pi-${digest}`;
}

function optionalArg(args: string[], flag: string, value: unknown): void {
	if (value !== undefined && value !== null) {
		args.push(flag, String(value));
	}
}

function optionalJson(args: string[], flag: string, value: unknown): void {
	if (value !== undefined && value !== null) {
		args.push(flag, JSON.stringify(value));
	}
}

async function executeBridge(
	piApi: ExtensionAPI,
	args: string[],
	signal: AbortSignal | undefined,
	ctx: { cwd: string },
) {
	const python = process.env.AIOS_PYTHON || "python";
	let processResult;
	try {
		processResult = await piApi.exec(python, [BRIDGE_PATH, ...args], {
			cwd: ctx.cwd,
			signal,
			timeout: DEPLOY_TIMEOUT_MS,
		});
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		return {
			content: [{ type: "text" as const, text: `Cloud bridge could not start: ${message}` }],
			details: { status: "error", error: message },
			isError: true,
		};
	}

	let payload: DeployPayload;
	try {
		payload = parsePayload(processResult.stdout);
	} catch (error) {
		const reason = error instanceof Error ? error.message : String(error);
		const diagnostics = tail(processResult.stderr.trim());
		const message = diagnostics ? `${reason}: ${diagnostics}` : reason;
		return {
			content: [
				{ type: "text" as const, text: `Cloud bridge returned invalid output: ${message}` },
			],
			details: {
				status: "error",
				error: message,
				exit_code: processResult.code,
				killed: processResult.killed,
			},
			isError: true,
		};
	}

	if ((processResult.code !== 0 || processResult.killed) && payload.status !== "error") {
		payload = {
			...payload,
			status: "error",
			error: processResult.killed
				? "cloud bridge was terminated"
				: `cloud bridge exited with code ${processResult.code}`,
		};
	}

	return {
		content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
		details: payload,
		isError: FAILURE_STATUSES.has(payload.status),
	};
}

function registerBridgeTool<TParams extends TSchema>(
	piApi: ExtensionAPI,
	spec: BridgeToolSpec<TParams>,
): void {
	piApi.registerTool(
		defineTool({
			name: spec.name,
			label: spec.label,
			description: spec.description,
			promptSnippet: spec.promptSnippet,
			promptGuidelines: spec.promptGuidelines,
			parameters: spec.parameters,
			executionMode: "sequential",
			async execute(toolCallId, params, signal, onUpdate, ctx) {
				const progress = spec.progress?.(params);
				if (progress) {
					onUpdate?.({ content: [{ type: "text", text: progress }] });
				}
				return executeBridge(piApi, spec.args(params, toolCallId), signal, ctx);
			},
		}),
	);
}

function registerDeployTools(pi: ExtensionAPI): void {
	registerBridgeTool(pi, {
		name: "deploy",
		label: "Deploy App",
		description:
			"Validate and upload the app rooted at aios.deploy.yaml, then enqueue one ordered, " +
			"idempotent AIOS cloud pipeline. The source path is always Pi's current workspace.",
		promptSnippet: "Deploy the current manifest-rooted app through the AIOS cloud pipeline",
		promptGuidelines: [
			"Create and validate aios.deploy.yaml before calling deploy; use the app_id supplied in the task.",
			"Declare only components the app actually contains. The pipeline orders database, server, then frontend.",
			"Never use provider CLIs, direct provider APIs, or project.json/local Docker deployment.",
			"After deploy returns a pipeline ID, call deployment_status until it completes or fails.",
			"On an actionable manifest or artifact error, fix the app and retry deploy.",
		],
		parameters: Type.Object({
			components: Type.Optional(
				Type.Array(
					Type.Union([
						Type.Literal("database"),
						Type.Literal("server"),
						Type.Literal("frontend"),
					]),
					{ minItems: 1, uniqueItems: true },
				),
			),
		}),
		args(params, toolCallId) {
			const args = ["deploy", "--operation-id", stableOperationId(toolCallId)];
			for (const component of params.components ?? []) {
				args.push("--component", component);
			}
			return args;
		},
		progress: () => "Uploading the current workspace and starting its cloud pipeline...",
	});

	registerBridgeTool(pi, {
		name: "deployment_status",
		label: "Deployment Pipeline Status",
		description: "Read the durable status of an AIOS cloud deployment pipeline.",
		promptSnippet: "Check whether an AIOS cloud deployment pipeline completed or failed",
		promptGuidelines: [
			"A queued or running pipeline is not complete; continue checking without claiming a live deployment.",
			"If the pipeline fails or requires action, report the exact cloud response.",
		],
		parameters: Type.Object({ pipeline_id: RESOURCE_ID("Pipeline ID returned by deploy") }),
		args: (params) => ["status", "--pipeline-id", params.pipeline_id],
		progress: (params) => `Checking pipeline ${params.pipeline_id}...`,
	});
}

function registerDeploymentLifecycleTools(pi: ExtensionAPI): void {
	registerBridgeTool(pi, {
		name: "get_deployment_status",
		label: "Deployment Status",
		description: "Get one component deployment's durable state, safe errors, and live URL.",
		parameters: Type.Object({
			deployment_id: RESOURCE_ID("Component deployment ID"),
		}),
		args: (params) => ["get-deployment", "--deployment-id", params.deployment_id],
	});

	registerBridgeTool(pi, {
		name: "get_deployment_events",
		label: "Deployment Events",
		description: "Read component deployment progress and action-required events after a cursor.",
		parameters: Type.Object({
			deployment_id: RESOURCE_ID("Component deployment ID"),
			after: Type.Optional(
				Type.Integer({ description: "Last event cursor, or -1 for the beginning", minimum: -1 }),
			),
		}),
		args(params) {
			const args = ["get-deployment-events", "--deployment-id", params.deployment_id];
			optionalArg(args, "--after", params.after);
			return args;
		},
	});

	for (const control of [
		{
			name: "cancel_cloud_deployment",
			label: "Cancel Cloud Deployment",
			action: "cancel-deployment",
			description: "Cancel a queued or running cloud deployment.",
		},
		{
			name: "resume_cloud_deployment",
			label: "Resume Cloud Deployment",
			action: "resume-deployment",
			description: "Resume a deployment after its requested action or secret exists.",
		},
		{
			name: "rollback_cloud_deployment",
			label: "Rollback Cloud Deployment",
			action: "rollback-deployment",
			description: "Redeploy a prior server or frontend artifact as an immutable release.",
		},
	] as const) {
		registerBridgeTool(pi, {
			name: control.name,
			label: control.label,
			description: control.description,
			promptGuidelines: [
				"Use deployment control tools only when the user requested that action or the deployment workflow explicitly requires it.",
			],
			parameters: Type.Object({
				deployment_id: RESOURCE_ID("Component deployment ID"),
			}),
			args: (params) => [control.action, "--deployment-id", params.deployment_id],
		});
	}
}

function registerAppTools(pi: ExtensionAPI): void {
	registerBridgeTool(pi, {
		name: "get_app_info",
		label: "App Info",
		description: "Get cloud app metadata, active component URLs, and latest component state.",
		parameters: Type.Object({ app_id: APP_ID }),
		args: (params) => ["get-app-info", "--app-id", params.app_id],
	});

	registerBridgeTool(pi, {
		name: "check_app_status",
		label: "Check App Status",
		description:
			"Check all app component phases plus artifact upload and verification state.",
		parameters: Type.Object({ app_id: APP_ID }),
		args: (params) => ["check-app-status", "--app-id", params.app_id],
	});
}

function registerMediaTools(pi: ExtensionAPI): void {
	registerBridgeTool(pi, {
		name: "upload_app_media",
		label: "Upload App Media",
		description:
			"Upload an image, video, or audio file from the current manifest-rooted workspace " +
			"through AIOS cloud private storage.",
		promptGuidelines: [
			"Only upload media belonging to the current app; the bridge rejects paths outside its workspace.",
		],
		parameters: Type.Object({
			app_id: APP_ID,
			local_path: Type.String({
				description: "Workspace-relative or in-workspace absolute media file path",
				minLength: 1,
				maxLength: 4096,
			}),
			destination: Type.Optional(
				Type.String({
					description: "Optional safe relative object destination",
					minLength: 1,
					maxLength: 512,
				}),
			),
			content_type: Type.Optional(
				Type.String({
					description: "Optional image/*, video/*, or audio/* MIME type",
					pattern: "^(image|video|audio)/[A-Za-z0-9.+-]+$",
				}),
			),
		}),
		args(params) {
			const args = [
				"upload-media",
				"--app-id",
				params.app_id,
				"--local-path",
				params.local_path,
			];
			optionalArg(args, "--destination", params.destination);
			optionalArg(args, "--content-type", params.content_type);
			return args;
		},
		progress: (params) => `Uploading media for ${params.app_id}...`,
	});

	registerBridgeTool(pi, {
		name: "list_app_media",
		label: "List App Media",
		description: "List verified private media objects stored for an app.",
		parameters: Type.Object({ app_id: APP_ID }),
		args: (params) => ["list-media", "--app-id", params.app_id],
	});

	registerBridgeTool(pi, {
		name: "get_app_media_url",
		label: "Get App Media URL",
		description: "Create a short-lived private download URL for an app media object.",
		parameters: Type.Object({
			app_id: APP_ID,
			media_id: RESOURCE_ID("Media object ID"),
			expires_in: Type.Optional(
				Type.Integer({
					description: "URL lifetime in seconds",
					minimum: 60,
					maximum: 86400,
				}),
			),
		}),
		args(params) {
			const args = [
				"get-media-url",
				"--app-id",
				params.app_id,
				"--media-id",
				params.media_id,
			];
			optionalArg(args, "--expires-in", params.expires_in);
			return args;
		},
	});

	registerBridgeTool(pi, {
		name: "delete_app_media",
		label: "Delete App Media",
		description: "Delete one app media object through AIOS cloud.",
		parameters: Type.Object({
			app_id: APP_ID,
			media_id: RESOURCE_ID("Media object ID"),
		}),
		args: (params) => [
			"delete-media",
			"--app-id",
			params.app_id,
			"--media-id",
			params.media_id,
		],
	});
}

function registerDatabaseTools(pi: ExtensionAPI): void {
	registerBridgeTool(pi, {
		name: "list_database_tables",
		label: "List Database Tables",
		description: "List app database tables and whether policy allows row reads.",
		parameters: Type.Object({ app_id: APP_ID }),
		args: (params) => ["list-database-tables", "--app-id", params.app_id],
	});

	registerBridgeTool(pi, {
		name: "inspect_database_table",
		label: "Inspect Database Table",
		description:
			"Inspect columns, constraints, indexes, row estimates, and migration history.",
		parameters: Type.Object({
			app_id: APP_ID,
			table: DATABASE_NAME("Table name"),
		}),
		args: (params) => [
			"inspect-database-table",
			"--app-id",
			params.app_id,
			"--table",
			params.table,
		],
	});

	registerBridgeTool(pi, {
		name: "query_database_table",
		label: "Query Database Table",
		description:
			"Run a policy-checked structured read-only table query. This tool never accepts SQL.",
		promptGuidelines: [
			"Use query_database_table only for structured reads; never attempt raw SQL or schema mutations through tools.",
		],
		parameters: Type.Object({
			app_id: APP_ID,
			table: DATABASE_NAME("Table name"),
			columns: Type.Optional(
				Type.Array(DATABASE_NAME("Column name"), { maxItems: 100, uniqueItems: true }),
			),
			filters: Type.Optional(
				Type.Array(
					Type.Object({
						column: DATABASE_NAME("Filter column"),
						op: Type.String({
							description: "Cloud-supported comparison operator such as eq",
							pattern: "^[a-z][a-z0-9_]{0,31}$",
						}),
						value: Type.Optional(Type.Unknown()),
					}),
					{ maxItems: 50 },
				),
			),
			order: Type.Optional(
				Type.Array(
					Type.Object({
						column: DATABASE_NAME("Order column"),
						direction: Type.Union([Type.Literal("asc"), Type.Literal("desc")]),
					}),
					{ maxItems: 20 },
				),
			),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 1000, default: 100 })),
		}),
		args(params) {
			const args = [
				"query-database-table",
				"--app-id",
				params.app_id,
				"--table",
				params.table,
			];
			optionalJson(args, "--columns-json", params.columns);
			optionalJson(args, "--filters-json", params.filters);
			optionalJson(args, "--order-json", params.order);
			optionalArg(args, "--limit", params.limit);
			return args;
		},
	});

	registerBridgeTool(pi, {
		name: "list_database_migrations",
		label: "List Database Migrations",
		description: "List immutable checksummed migrations applied to an app database.",
		parameters: Type.Object({ app_id: APP_ID }),
		args: (params) => ["list-database-migrations", "--app-id", params.app_id],
	});
}

export default function (pi: ExtensionAPI) {
	registerDeployTools(pi);
	registerDeploymentLifecycleTools(pi);
	registerAppTools(pi);
	registerMediaTools(pi);
	registerDatabaseTools(pi);
}
