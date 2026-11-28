import { RfpTicketPage } from "@/components/rfp/RfpTicketPage";

type RfpTicketRouteProps = {
  params: Promise<{ id: string }>;
};

export default async function RfpTicketRoutePage({ params }: RfpTicketRouteProps) {
  const { id } = await params;

  return <RfpTicketPage ticketId={id} />;
}
