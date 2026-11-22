import { Suspense } from "react";
import { Shortlist } from "@/components/Shortlist";

async function ShortlistFromParams({ params }: { params: PageProps<"/jobs/[id]/shortlist">["params"] }) {
  const { id } = await params;
  return <Shortlist jobId={id} />;
}

export default function ShortlistPage({ params }: PageProps<"/jobs/[id]/shortlist">) {
  return (
    <Suspense fallback={<p className="p-6 text-sm text-quiet">Loading the shortlist…</p>}>
      <ShortlistFromParams params={params} />
    </Suspense>
  );
}
