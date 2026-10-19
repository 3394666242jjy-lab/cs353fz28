import analysis from '../../../examples/analysis-response.json';
import evidence from '../../../examples/evidence-responses.json';
import resumeText from '../../../examples/resume-demo.md?raw';
import projectText from '../../../examples/project-demo.md?raw';
import jdText from '../../../examples/jd-backend-intern.txt?raw';
import type { components } from './schema';

export type AnalysisResult = components['schemas']['AnalysisResult'];
export type Evidence = components['schemas']['Evidence'];

// This boundary is checked against the Pydantic models by backend fixture tests.
// Keep mock data out of live API handlers and the personal document index.
export const sampleAnalysis = analysis.data as AnalysisResult;
export const sampleEvidence = Object.values(evidence.responses).map(
  (entry) => entry.data as Evidence,
);
export const sampleInputs = { resumeText, projectText, jdText };
