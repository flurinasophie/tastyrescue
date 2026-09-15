# Part 01 — Team and Pitch

## 1. Team

Flurina Baumbach
Gregory von Werne
Annabel von Morgenstern 


## 2. Business idea "TastyRescue" (Too Good To Go, for Thailand)

TastyRescue connects Thailand's restaurants, bakeries, cafés, and hotel buffets
with everyday customers who want great food at a steep discount, right before
it would otherwise be thrown out. Every afternoon and evening, vendors post a
"surprise bag" of unsold food for about a third of the retail price. Customers browse a map of
nearby bags, reserve one in the app, pay online, and pick it up in a short
window before closing.

**Who pays:** the customer, at reservation time, through the app.
**What happens a million times a day (at scale):** a bag gets posted, browsed,
reserved, or picked up — across tens of thousands of restaurants, cafés, and
convenience-store branches nationwide, concentrated into the lunch and dinner
closing windows.

Vendors turn food that would be wasted into extra revenue and new foot
traffic; customers get affordable, good food; TastyRescue takes a small
commission on every transaction.

## 3. Conceptual ER diagram

 

## 4. Entities and expected order of magnitude

| Entity | What it represents | Order of magnitude (one sentence) |

| **User** | A customer with an account | ~3–5 million registered users within 3 years, growing by 5,000–10,000 signups/day during marketing pushes. |
| **Vendor** | A restaurant/café/bakery brand | ~50,000–80,000 vendor accounts nationwide, growing slowly (tens to low hundreds/day) since onboarding is sales-driven, not viral. |
| **Branch** | One physical outlet of a vendor | ~100,000–150,000 branches once large chains (convenience stores, bakery chains) join, growing in step with vendor sign-ups × outlets per vendor. |
| **Bag** | One "surprise bag" listing posted by a branch | **The big one:** 3–8 new listings per active branch per day → 300,000–1,000,000+ new rows/day nationwide, hundreds of millions/year, arriving in sharp bursts around 11:00–13:00 and 18:00–20:00. |
| **Reservation** | A customer reserving (buying) a bag | ~150,000–500,000/day at scale (40–60% of bags sell), with the same extreme intra-day bursts as Bag. |
| **Payment** | The financial transaction for a reservation | Tracks 1:1 with Reservation (same order of magnitude), but must be retained indefinitely for tax/audit purposes — it only ever grows. |
| **Review** | Optional customer feedback after pickup | ~20–30% of reservations get reviewed — tens of thousands of new rows/day, growing far more slowly than Bag/Reservation/Payment. |

## 5. Viva prep — "Which entity will kill you first, and why?"

**Bag will kill me first.**

Volume: it's the biggest table by far, hundreds of thousands of new rows a day.
Bursts: almost all of it lands in two ~2-hour windows (lunch, dinner), so peak traffic is way higher than the daily average suggests.
Conflicting lifecycle: each bag is only useful for a few hours (needs to expire fast), but I can't delete it because I need the history for reporting. So it has to act both temporary and permanent at the same time.