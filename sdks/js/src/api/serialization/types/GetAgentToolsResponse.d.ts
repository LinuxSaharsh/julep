/**
 * This file was auto-generated by Fern from our API Definition.
 */
import * as serializers from "..";
import * as JulepApi from "../../api";
import * as core from "../../core";
export declare const GetAgentToolsResponse: core.serialization.ObjectSchema<
  serializers.GetAgentToolsResponse.Raw,
  JulepApi.GetAgentToolsResponse
>;
export declare namespace GetAgentToolsResponse {
  interface Raw {
    items?: serializers.Tool.Raw[] | null;
  }
}