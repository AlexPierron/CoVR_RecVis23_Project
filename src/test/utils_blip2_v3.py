import datetime
import time

import einops
import numpy as np
import torch
import torch.nn.functional as F


@torch.no_grad()
def evaluate_blip2(model, data_loader, fabric):
    model.eval()

    fabric.print("Computing features for evaluation...")
    start_time = time.time()

    tar_img_feats = []
    query_feats = []
    captions = []
    pair_ids = []

    for ref_img, tar_feat, caption, pair_id, *_ in data_loader:
        pair_ids.extend(pair_id.cpu().numpy().tolist())
        captions.extend(caption)

        device = ref_img.device

        ref_img = ref_img.to(torch.float32)

        ref_img_embeds = model.ln_vision(model.visual_encoder(ref_img))

        ref_img_atts = torch.ones(ref_img_embeds.size()[:-1], dtype=torch.long).to(device)

        query_tokens = model.query_tokens.expand(ref_img_embeds.shape[0], -1, -1)


        # Encode the target image
        tar_feat = tar_feat.to(device)
        tar_img_feat = F.normalize(tar_feat, dim=-1)
        
        if model.pooling == "max":
            tar_img_feat_pool, _ = torch.max(tar_img_feat, dim=1)

        elif model.pooling == "mean":
            tar_img_feat_pool, _ = torch.mean(tar_img_feat, dim=1)


        # Image-text Matching
        text_tokens = model.tokenizer(
            caption,
            padding="max_length",
            truncation=True,
            max_length= model.max_txt_len,
            return_tensors="pt",
        ).to(device)

        # Try the following if yours does't work. If both work please evaluate the difference between the two and add it to the report.
        # query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(
        #    self.device
        #)
        #My on attention_mask : text_tokens.attention_mask
        # attention_mask = torch.cat([query_atts, text_tokens.attention_mask], dim=1)
        
        query_atts = torch.ones(query_tokens.size()[:-1], dtype=torch.long).to(self.device)

        output = model.Qformer.bert(
            text_tokens.input_ids,
            query_embeds=query_tokens,
            attention_mask=torch.cat([query_atts, text_tokens.attention_mask], dim=1),
            encoder_hidden_states=ref_img_embeds,
            encoder_attention_mask=ref_img_atts,
            return_dict=True,
        )

        query_feat = output.last_hidden_state[:, : query_tokens.size(1), :]
        
        query_feat = F.normalize(model.text_proj(query_feat), dim=-1) 
        if model.pooling == "max":
            query_feat_pool, _ = torch.max(query_feat, dim=1)

        elif model.pooling == "mean":
            query_feat_pool, _ = torch.mean(query_feat, dim=1)

        query_feats.append(query_feat_pool.cpu())

        # Encode the target image
        tar_img_feats.append(tar_img_feat_pool.cpu())

    query_feats = torch.cat(query_feats, dim=0)
    tar_img_feats = torch.cat(tar_img_feats, dim=0)

    query_feats = F.normalize(query_feats, dim=-1)
    tar_img_feats = F.normalize(tar_img_feats, dim=-1)

    ref_img_ids = [data_loader.dataset.pairid2ref[pair_id] for pair_id in pair_ids]
    tar_img_ids = [data_loader.dataset.pairid2tar[pair_id] for pair_id in pair_ids]

    ref_img_ids = torch.tensor(ref_img_ids, dtype=torch.long)
    tar_img_ids = torch.tensor(tar_img_ids, dtype=torch.long)

    if fabric.world_size > 1:
        # Gather tensors from every process
        query_feats = fabric.all_gather(query_feats)
        tar_img_feats = fabric.all_gather(tar_img_feats)
        ref_img_ids = fabric.all_gather(ref_img_ids)
        tar_img_ids = fabric.all_gather(tar_img_ids)

        query_feats = einops.rearrange(query_feats, "d b e -> (d b) e")
        tar_img_feats = einops.rearrange(tar_img_feats, "d b e -> (d b) e")
        ref_img_ids = einops.rearrange(ref_img_ids, "d b -> (d b)")
        tar_img_ids = einops.rearrange(tar_img_ids, "d b -> (d b)")

    if fabric.global_rank == 0:
        sim_q2t = (query_feats @ tar_img_feats.t()).cpu().numpy()

        # Add zeros where ref_img_id == tar_img_id
        for i in range(len(ref_img_ids)):
            for j in range(len(tar_img_ids)):
                if ref_img_ids[i] == tar_img_ids[j]:
                    sim_q2t[i][j] = -10

        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print("Evaluation time {}".format(total_time_str))

        eval_result = eval_recall(sim_q2t)
        fabric.print(eval_result)

        fabric.log_dict(
            {
                "val/R1": eval_result["R1"],
                "val/R5": eval_result["R5"],
                "val/R10": eval_result["R10"],
                "val/R_mean": eval_result["R_mean"],
            }
        )

    fabric.barrier()


@torch.no_grad()
def eval_recall(scores_q2t):
    # Query->Target
    ranks = np.zeros(scores_q2t.shape[0])

    for index, score in enumerate(scores_q2t):
        inds = np.argsort(score)[::-1]
        ranks[index] = np.where(inds == index)[0][0]

    # Compute metrics
    tr1 = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)  # type: ignore
    tr5 = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    tr10 = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)
    tr50 = 100.0 * len(np.where(ranks < 50)[0]) / len(ranks)

    tr_mean = (tr1 + tr5 + tr10) / 3

    eval_result = {
        "R1": round(tr1, 4),
        "R5": round(tr5, 4),
        "R10": round(tr10, 4),
        "R50": round(tr50, 4),
        "R_mean": round(tr_mean, 4),
    }
    return eval_result
