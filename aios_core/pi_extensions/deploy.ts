/**
 * Trusted mini-aios deployment tool for Pi.
 *
 * Load explicitly while extension discovery is disabled:
 *
 *   pi --no-extensions -e /app/aios_core/pi_extensions/deploy.ts ...
 *
 * The Python bridge accepts no source path. This extension runs it with Pi's
 * exact working directory, so deployment is limited to the project Pi is
 * currently editing.
 */

import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type DeployPayload = Record<string, unknown> & {
	status: string;
	error?: string;
	logs?: string;
};

const BRIDGE_PATH = resolve(dirname(fileURLToPath(import.meta.url)), "../deploy/pi_bridge.py");
const DEPLOY_TIMEOUT_MS = 5 * 60 * 1000;
const OUTPUT_TAIL_LIMIT = 4000;

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

function createDeployTool(piApi: ExtensionAPI) {
	return defineTool({
		name: "deploy",
		label: "Deploy App",
		description:
			"Build and run the project in Pi's current working directory. The project must contain project.json. " +
			"Returns a URL on success or structured error details and container logs on failure.",
		promptSnippet: "Deploy the current project and return its live URL or actionable logs",
		promptGuidelines: [
			"Use deploy only after creating project.json in the current project directory.",
			"When deploy returns status error, inspect its error and logs, fix the project, and call deploy again.",
		],
		parameters: Type.Object({
			slug: Type.String({
				description: "Stable app name: 1-63 lowercase letters, digits, or interior hyphens",
				minLength: 1,
				maxLength: 63,
				pattern: "^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
			}),
		}),
		executionMode: "sequential",

		async execute(_toolCallId, params, signal, onUpdate, ctx) {
			onUpdate?.({
				content: [{ type: "text", text: `Deploying ${params.slug} from ${ctx.cwd}...` }],
			});

			const python = process.env.AIOS_PYTHON || "python";
			let processResult;
			try {
				processResult = await piApi.exec(python, [BRIDGE_PATH, "--slug", params.slug], {
					cwd: ctx.cwd,
					signal,
					timeout: DEPLOY_TIMEOUT_MS,
				});
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				return {
					content: [{ type: "text", text: `Deploy bridge could not start: ${message}` }],
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
					content: [{ type: "text", text: `Deploy bridge returned invalid output: ${message}` }],
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
						? "deploy bridge was terminated"
						: `deploy bridge exited with code ${processResult.code}`,
				};
			}

			const rendered = JSON.stringify(payload, null, 2);
			return {
				content: [{ type: "text", text: rendered }],
				details: payload,
				isError: payload.status !== "running",
			};
		},
	});
}

export default function (pi: ExtensionAPI) {
	pi.registerTool(createDeployTool(pi));
}
